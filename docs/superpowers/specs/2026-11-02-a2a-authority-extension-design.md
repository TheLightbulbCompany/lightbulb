# A2A as the protocol, authority as a required extension: design

**Status:** APPROVED by the founder 2 Nov 2026 (phase 1 as cut in §2a; third-party OAuth clients are phase 1b). Nothing is built yet; the plan is docs/superpowers/plans/2026-11-02-a2a-authority-phase1.md. Ponytail pass applied 2 Nov (§2a). Founder answers 2 Nov: the workspace admin holds authority over a hosted agent; the extension spec is published at launch and proposed to the A2A project later.
**Decision this implements (2 Nov 2026):** governance is built incrementally, planned first, tested, coordinated with the Workers session, and the first increment is A2A. A2A (Linux Foundation, v1.0) becomes the network's protocol; our peer-to-peer transport becomes an A2A binding later; the authority object is a *required* A2A extension; the hosted workspace member speaks A2A over HTTP first.
**Research it rests on:** `docs/research/2026-11-01-org-auto-mode-governance.md` and its strands 01-11 (PR #4551).

## 1. Intent

An agent that acts for one person and asks another person's agent or computer to do something must carry, on every hop, a verifiable statement of who asked, through which agents, for whom, and exactly what. A point the company controls decides whether that request is allowed, by rules the company wrote, and records which rule decided. Out-of-rule requests go to the person who holds authority over the resource, not to the agent's owner. People see escalations and the record, never per-call prompts.

Why A2A: it is the interop protocol the deployed-agent vendors are converging on, it has a formal extension mechanism designed for things like message signing, and it explicitly leaves authorization to the implementation (spec §13.1: "Authorization boundaries are defined by each agent's authorization model, not prescribed by the protocol"). It has no delegation or on-behalf-of mechanism. That hole is this product.

## 1a. Precedent (2 Nov 2026)

DeepSeek's `dsh-superagent-prototype` (`docs/002-authorization.md`) ships host-enforced Mandates inside one host: "The host enforces allowed lists, rules and Mandates"; messages "are inputs to a decision; they cannot change those permissions"; explicit versus inferred admission; an `observe` mode that records "would accept" without accepting; context-only subscriptions whose delivery "grants no new authority"; revalidation before a ticket resumes; a policy version whose change "discards running Evals and queues their Triggers again". It does not address delegation across people, organizations or agent boundaries, and its decisions are recorded but not signed. So the honest claim is narrower than "nobody does mandates": no shipped system carries a signed, operand-bound authority object across people and vendors and escalates to the resource's holder. Three of its rules are adopted here: delivery grants no authority (§4), revalidation before resume (§5 step 4), and a policy revision change re-decides held tasks (§6).

## 2. Goals, non-goals, anti-goals

Goals (this increment):
- Any A2A client that can authenticate to the workspace can send a task to a hosted workspace agent (Friday) at a Lightbulb URL, and the request is refused unless it activates our authority extension and states the operation and intent in it. The hosted door mints and signs the authority object from the verified identity plus that statement (§4).
- A hosted agent's request to a person's laptop session carries the same object, is decided by the workspace's rules, and is held for the session's person when the rules do not decide.
- Every decision is recorded, signed, and retrievable with the task.
- An external party can verify the record with our published key.

Non-goals (later increments): migrating the P2P network's frames to the A2A data model (phase 2); compiling company sentences into rules with an LLM; spend mandates; adapters for Slack, Notion, Copilot Studio; a classifier of any kind.

Anti-goals (a failure even if it works): a dialog to the agent's owner for every request; an approvals inbox as the product; any path where the extension can be omitted and the request still runs; an unsigned record; a bespoke protocol that only Lightbulb speaks.

## 2a. Phase 1 after the ponytail pass (supersedes anything fuller below)

Cut from phase 1, with the trigger to add it back:
- `a2a-sdk` server, task store and event queues: use the package for its generated types only; the four handlers are ours in the existing FastAPI router pattern. Add the SDK server when a second binding is needed.
- REST binding and `SendStreamingMessage`: JSON-RPC only, card says `streaming: false`. Add when a client needs it.
- `CancelTask`: return `UnsupportedOperationError` (`-32004`). Add when someone cancels.
- Agent Card signatures: the card is served over our HTTPS; `signatures` omitted. Add when a design partner or the A2A project asks.
- Second Clerk OAuth application, protected-resource metadata and the generalized verifier: phase 1 accepts Clerk session JWTs only (`core/auth.py:211 get_current_user`), so "any A2A client" in phase 1 means any client holding a Lightbulb sign-in for the org, including the conformance test client. Third-party OAuth clients are phase 1b, when the first external client exists.
- `prev_sig` and the narrowing check: every phase-1 hop is minted by the backend in one signature over the whole chain. Add both when laptops sign their own hops (phase 2).
- `when` clauses on rules (`operands_equal`, `intent_matches`): no acceptance test needs them. Rules are resource, skill, requesters, effect. Add when a workspace writes the first rule that needs one.
- Re-deciding held tasks on a policy revision change: a revision change cancels held tasks (`CANCELED`, reason `policy_changed`). One line. Re-decide when a workspace asks.
- Sweepers for hold expiry and undeliverable commands: both computed at read time from timestamps in `GetTask`. No cron.
- A new Approve / Deny UI: reuse the existing approval card in the app (the Apps provisioning card from #4556 is the newest), restricted to the holder. The builder confirms the card module; if restriction to one person is not possible there, a DM with two signed links is the fallback.

Reused from the codebase (rung 2): the proof issuer that signs sign-in proofs (one JWS helper signs the authority object and the record); `hosted_mcp.py` for the "which person and org" context; `messaging.create_human_message` and the DM-room-if-needed pattern for dispatch to Friday; `post_agent_message` with `request_id` idempotency (`_pin_destination`) for linking the reply; `native_session_tools.session_send` for laptop delivery; DynamoDB tables with TTL for records and replay ids; `_public_ingress` rate buckets for the card route.

Shortest path to the §10 tests, in build order: (1) card route plus `SendMessage` and `GetTask` with an allow-all-in-org rule → test 1; (2) mint, verify, record, `keys.json` → tests 2 and 6; (3) the rule document with `holders`, `rules`, `default`, the hold state, the holder's answer through the existing card, no self-approval → tests 3 and 4; (4) the exception row → test 5; (5) the `ask_session` tool and internal route → test 3 end to end; (6) the replay script on the Codex subscription → test 7.

## 3. Constraints

- A2A v1.0 data model and rules (spec sections 4, 4.6, 7.6, 12, 13, 14). Breaking changes to the extension need a new URI (§4.6.3).
- The only hosted HTTP ingress is the FastAPI backend (`apps/backend/main.py:567`, behind `api.thelightbulb.company`). The CLI daemon has no HTTP listener (`daemon/mod.rs:282`, `member.rs:85`, `ipc.rs:66` are Unix sockets only), and the bridge that would call the cloud member's `app.send` is not in the monorepo's main.
- P2P envelopes are unsigned (`net/frames.rs:94`), carry no `metadata` field, and `kind` may only be `message` (`ipc.rs:1303`). Changing them is the phase-2 migration, not this increment.
- Identity stays ours: Clerk OAuth for clients and people at the backend (`hosted_mcp.py:37`), sign-in proofs for computers (`proof.rs`), Ed25519 computer keys. Agent Cards and records are signed with the proof issuer key the backend already holds.
- No new policy language. Rules are a small JSON document per workspace in v1; Cedar or OPA export is a later increment.
- Secrets never printed; no production data read without the founder's OK; fresh worktrees from `origin/main`; draft PRs; no release without the founder.
- Workers: the Workers session keeps testing what exists and designs no approval flow. The `Meta` map on its branch (`workers.rs:21` on `feat/network-workers`) becomes the A2A `Message.metadata` in phase 2.

## 4. The extension

**URI:** `https://thelightbulb.company/a2a/ext/authority/v1`
**Declared** in every Lightbulb Agent Card under `capabilities.extensions` with `required: true`. Per spec, an agent "should reject the incoming request with an appropriate error" when a client does not activate a required extension. Activation is the `A2A-Extensions` header (JSON-RPC and REST bindings) and the `extensions` array on the Message.
**Carried** in `Message.metadata` under the key equal to the URI, and the URI listed in the Message's `extensions` array. The extensions guide says extensions should put custom attributes in `metadata` keyed by URI rather than adding fields to core structures; the spec's own examples use that pattern.

The object (JSON, signed as a compact JWS, EdDSA):

```
{
  "v": 1,
  "id": "<uuid>",                       // one per request; nonce for replay protection
  "iat": <unix>, "exp": <unix>,         // exp - iat <= 24h
  "requester": {                        // the person at the origin, verified
    "person": "user_...", "org": "org_...",
    "verified_by": "clerk-oauth" | "lightbulb-proof"
  },
  "chain": [                            // origin first; later hops append, never edit
    { "agent": "<agent card url or member key>", "session": "<id|null>", "person": "user_...", "at": <unix> }
  ],
  "on_behalf_of": "user_..." | null,    // when the requester acts for someone else; v1 may only equal requester
  "operation": {
    "skill": "<A2A skill id on the target card>",
    "resource": "<urn: agent | session | room>",
    "operands": { ... },                // the exact arguments the target will act on
    "operands_hash": "sha256:<hex>"     // over RFC 8785 canonical JSON of operands
  },
  "intent": "<the person's own words, <= 2048 chars>",
  "policy": null                        // filled by the decision point; see §6
}
```

Who mints and signs (my call, settles the reviewer's first question): in phase 1 only the hosted door mints. A client activates the extension and sends, under the URI key, an unsigned *authority request*: `{ "operation", "intent", "on_behalf_of" }`. The backend verifies the caller (§5), fills `requester` and `chain[0]` from the verified identity, computes `operands_hash`, signs, and from then on the signed object is what travels and what the record references. A client that sends a pre-signed object from a registered key (a laptop daemon with its computer key, phase 2) is verified instead of re-minted. So "required extension" means: no authority request, no task. Each later hop appends to `chain` and re-signs the whole object; the previous signature is kept as `prev_sig` so the chain is verifiable end to end. In phase 1 every hop is the backend, so a chain has at most two entries (the origin person, then the hosted agent), both written by the backend in one mint; `prev_sig` is present but trivially the same signer.

Narrowing check, run by every verifier: hop N's `operation.skill` equals hop N-1's; every key in hop N's `operands` exists in hop N-1's with an equal value, or hop N-1 declared that key as `"*"`; hop N may drop keys, never add them. A hop that fails the check is rejected as widened.

Verification at the target, in order: signature by a known key; `exp`; `id` unseen (a conditional put into the record table with a 24h TTL; no separate replay store); `chain[0].person == requester.person`; `operands_hash` matches the operands the target is about to act on; the Message's `extensions` array lists the URI. A missing or unactivated extension is rejected with A2A `ExtensionSupportRequiredError` (`-32008`, spec §5.4). A present but invalid object is rejected with `InvalidParamsError` (`-32602`) and a status message naming the check, never the secret.

The spec's security rules apply: extension data is untrusted input, validated rigorously; the extension "MUST NOT provide a way to bypass the agent's primary security controls" (it is the control, applied after authentication, never instead of it). Delivery grants no authority: receiving a message, a room post, a task or a subscription never authorizes anything; only a verified object does, and the text of a message is an input to the decision, never a change to the rules.

## 5. The endpoint (hosted, FastAPI)

New router `apps/backend/routers/a2a.py`, included in `main.py` before the MCP catch-all mount at `main.py:1123`. Python `a2a-sdk` with the FastAPI integration for the JSON-RPC and REST bindings; our code is the request handler, the extension check, the decision, and the record.

Routes (my call; the builder may regroup):
- `GET /a2a/{org}/{agent}/agent-card.json`: the signed Agent Card. Discovery in v1 is direct configuration (spec's "direct configuration" mode): the workspace admin copies the card URL from the app and gives it to the client; the domain-wide `/.well-known/agent-card.json` is not served in v1 because one domain hosts many workspaces and the spec prescribes no registry API. `supportedInterfaces[0] = { url: ".../a2a/{org}/{agent}", protocolBinding: "JSONRPC", protocolVersion: "1.0", tenant: "{org}" }`. `securitySchemes`: OAuth2 bearer via Clerk. The hosted MCP's verifier (`hosted_mcp.py:37`) hard-codes audience `.../mcp` and scope `lightbulb:messages:read` (`hosted_mcp.py:34, 69, 72`) and rejects JWTs; the builder generalizes it to take audience and scopes as parameters, registers a second Clerk OAuth application and protected-resource metadata for `{PUBLIC_API_BASE}/a2a` with scopes `lightbulb:a2a:send` and `lightbulb:a2a:read`, and also accepts a Clerk session JWT (`core/auth.py:211 get_current_user`) so the Lightbulb app can call the same door. `capabilities.extensions = [ {uri, required: true, description} ]`. `skills`: `message` (send the agent a message and get its reply), `ask_session` (ask one of the workspace's laptop sessions; v1 exposes this only to workspace agents, not external clients). `signatures`: JWS per §8.4 with `kid` = the issuer key id, canonicalized per RFC 8785.
- `POST /a2a/{org}/{agent}`: JSON-RPC v1.0 methods `SendMessage`, `SendStreamingMessage`, `GetTask`, `CancelTask` (spec §9.1; the `message/send` names are v0.3). REST equivalents from the SDK.
- `POST /api/v1/internal/a2a/{ownerId}/ask-session` (hook-token route, `messaging.py:765` pattern): how a hosted agent starts a hop. Friday's Hermes plugin gets a tool `ask_session` beside `send_room_message` (`plugins/lightbulb/__init__.py:710`) that posts the origin message id, the target session, the operation and the intent here. The backend resolves the origin message's author to `requester.person`, appends Friday as `chain[1]`, mints, decides, and dispatches (§8). An autonomous or scheduled turn has no origin message; then `requester.person` is the agent's holder (§6) and `verified_by` is `agent-runtime`. Friday never speaks A2A over HTTP back into the backend in v1.
- `GET /a2a/{org}/{agent}/tasks/{id}/record`: every signed decision record for the task, oldest first; `Task.metadata[uri].record` carries only the latest.
- `POST /a2a/{org}/{agent}/tasks/{id}/decide`: the holder's answer to a held task (allow or deny, with a reason). Only the holder's verified identity may call it; the requester may never answer their own request.
- `GET /a2a/keys.json`: the public keys (JWKS) that sign cards and records.

Task lifecycle:
1. Authenticate (Clerk bearer, org membership) or reject `401/403`.
2. Require the extension (header or `Message.extensions`) or reject per §4.6.3.
3. Mint or verify the object (§4): an authority request from an authenticated client is minted; a pre-signed object is verified. No authority request → `-32008`.
4. Decide (§6): `allow` → the task returns `TASK_STATE_WORKING` at once (`SendMessage` does not block; `SendStreamingMessage` streams status), and the message is dispatched by today's path (`messaging.create_human_message` → `openclaw_work._dispatch_message` → Hermes `/plugins/lightbulb-channel/inbound/{agentId}`). `create_human_message` needs a room (`messaging.py:4022`): `contextId` maps to one DM room per (requester person, agent), created on first use, and the task id is passed as the message's `request_id`. Friday's reply arrives through `post_agent_message` (`messaging.py:1347`) with `reply_to` equal to that id; the backend links it, stores it as an Artifact and marks the task `COMPLETED`. `deny` → `TASK_STATE_REJECTED` with the rule named. `hold` → `TASK_STATE_AUTH_REQUIRED` with a status message "waiting for <holder display name>" and `Task.metadata[uri].state = "held"`; the holder is notified (§7); on allow the object is re-verified (`exp`, replay, and the policy revision it was decided under) before the task continues; if the workspace's policy revision changed while the task was held, it is decided again under the new revision and the holder's answer is kept only if the new decision is still `hold`; on deny it is `REJECTED`, on expiry (24h) it is `CANCELED`.
5. Record (§6) at every state change: one record each for the decision, a holder's answer, completion, failure and expiry.
6. Offline target (laptop session): the command queues for up to `OFFLINE_SEND_SECONDS` (24h, `local_sessions.py`); the task stays `WORKING` until delivered, then `FAILED` with reason `undeliverable`. The hold expiry is separate and starts only when the task is held.

Spec note on the held state: §7.6 defines `TASK_STATE_AUTH_REQUIRED` for an agent that needs the client to supply authorization, and §7.6.4 says the transition is never itself an authorization. We use it for "a person who holds the resource must authorize", which is the same shape; the substate rides in metadata as a profile extension allows. My call; a reviewer may prefer `INPUT_REQUIRED`.

## 6. The decision and the record

Rules v1 are a JSON document per workspace, stored beside the company policy map (`billing_repo.py:1446` holds `CAPABILITY_POLICY_KEYS`; that map stays unenforced and untouched):

```
{
  "revision": "<ulid>",
  "holders": {
    "agent:friday": ["user_<workspace admin>"],
    "session:*": ["@session-person"],
    "room:*": ["user_<workspace admin>"]
  },
  "rules": [
    { "id": "r1", "resource": "agent:friday", "skill": "message",
      "requesters": ["org:org_..."], "effect": "allow" },
    { "id": "r2", "resource": "session:*", "skill": "ask_session",
      "requesters": ["agent:friday"], "when": {"intent_matches": "status of my own session"}, "effect": "allow" },
    { "id": "r3", "resource": "session:*", "skill": "ask_session",
      "requesters": ["agent:friday"], "effect": "hold" }
  ],
  "default": "hold"
}
```

Evaluation is deterministic and has two steps. First, any matching rule with effect `deny` wins, wherever it sits (Cedar's forbid-overrides-permit). Second, among the rest, document order, first match wins. No match → `default`. A rule matches when its `resource` (exact or `*` suffix), `skill` and `requesters` (a person id, `org:<id>`, or `agent:<id>`) all match. No `when` clause in phase 1 (§2a); no model is consulted. The example's `r2` is therefore phase 1b. `@session-person` resolves to the target session's person as `local_sessions` records it. Requesters outside the org → `deny` always. `on_behalf_of` ≠ requester → `deny` in v1.

A change of `revision` cancels every held task with reason `policy_changed` (the one-line form of DeepSeek's rule; re-deciding is phase 1b). A held task's answer becomes a time-boxed exception row: `{rule: "exception", holder, task, operands_hash, expires}`; a later identical request within the window is allowed by that row and recorded as such. Exceptions never widen beyond the requester and the operands hash (whole-branch review, 2 Nov: the key is resource, skill, requester, operands hash).

The record, one per decision, signed with the issuer key:

```
{ "task": "<id>", "authority_id": "<id>", "authority_hash": "sha256:...",
  "decision": "allow" | "deny" | "hold" | "allow_by_holder" | "deny_by_holder" | "expired",
  "rule": "r2" | "exception:<id>" | "default",
  "policy_revision": "<ulid>", "holder": "user_..." | null, "answered_by": "user_..." | null,
  "decided_at": <unix>, "input": { "requester", "chain", "operation", "intent" } }
```

`input` is the snapshot the decision saw (OPA's decision-log shape). Records are append-only and readable by the workspace admin, the requester, and the holder.

## 7. Escalation to the holder

The holder is looked up from `holders`. For `session:*` it is the session's person as the backend knows it (`local_sessions`). Notification in v1: a direct message to the holder in the app with the intent, the operation and operands, the requester chain, and Approve / Deny, which call `/decide` with the holder's own Clerk session. Holder == requester (a verified person asking for what they hold): the request is allowed with rule `holder`; nobody can answer their own hold at `/decide` (403 `self`), except the stand-in requester of an agent-run ask (`verified_by: agent-runtime`). (Ruling 2 Nov 2026, founder may overturn; it replaces "the task is `REJECTED` with rule `self`".) The agent's owner is informed by a message, not asked. A native dialog on the holder's Mac (the shape CLI PR #115 used for `team.join`) is phase 2, when the daemon signs its own hops.

## 8. Dispatch to a laptop session

Correction after reading `origin/main` (the earlier code map read a checkout 363 commits behind): the hosted door already reaches a laptop session through the network, not the desktop relay. `hosted_mcp.send_message` with a `session_id` forwards an `app.send` through the origin person's computer link (`hosted_mcp._through_member`, `hosted_mcp.py:651`; `network_route.app_call`, `network_route.py:142`), and the daemon delivers it to the session as "<client> via <person>". Phase 1 uses that path below the MCP layer (whole-branch review, 2 Nov: `_through_member` needs an MCP access token, so the task calls `network_route.best_link` and `network_route.app_call` directly): the send rides the origin person's link, `sender=(agent_id, agent_name)`, `request_id = task id`, and `text` carries a header of at most 512 bytes (requester, for whom, the first 200 characters of the intent, the record URL) before the message; the CLI caps `text` at 64 KiB and defangs it. If the origin person has no linked computer, the task fails `undeliverable` at once, with a record. The session does not verify the object in v1; the backend did. Phase 2 moves the send to the cloud member's bridge and the daemon verifies.

## 9. Phase 2 outline (not this increment, written so phase 1 does not paint us in)

- `Frame::Envelope` gains an A2A `Message` body: `messageId`, `role`, `parts`, `contextId`, `taskId`, `metadata`, `extensions`. Old `text` maps to one `TextPart`. Envelopes get a `sig` by the computer key (the ponytail note at `frames.rs:94` anticipates it). Unknown-variant tolerance is added before the change ships, one version ahead.
- The daemon serves and consumes Agent Cards over the network (a card per member, signed with the computer key, distributed with the roster) and declares the P2P binding in the card per §12.
- Holds `Kind::Message` is revived as the local `AUTH_REQUIRED` state; the signed `approvals` record (`holds.rs:244`) becomes the record for laptop-side decisions, with the same fields as §6.
- Workers: `assign` becomes a task with the authority object; `Policy { Allow, Ask, Refuse }` rows become rules in the workspace document.
- The cloud member's bridge (`app.send` via `Caller::Instance`) is the hosted A2A server's path into the network, replacing §8.

## 10. Tests (phase 1 acceptance)

1. Conformance: the official `a2a-sdk` Python client fetches the card, sends `SendMessage` with the extension, and gets a `COMPLETED` task with Friday's reply; the same without the extension gets the required-extension error.
2. Tampering: an object with a changed operand, an expired `exp`, a replayed `id`, a chain whose origin was edited, and a bad signature each produce a rejection naming the check.
3. Hold: Friday asks a laptop session something not covered by a rule; the task goes `AUTH_REQUIRED`; the holder approves in the app; the task completes; the record shows `allow_by_holder` with the holder's id; the requester cannot call `/decide`.
4. Self-approval: holder == requester → `allow`, rule `holder` (ruling 2 Nov 2026, founder may overturn; was `REJECTED`, rule `self`).
5. Exception window: a second identical request within the window is `allow` by `exception:<id>`; a request with different operands is held again.
6. Record verification: a script outside the backend fetches `/a2a/keys.json` and verifies a record's JWS and the card's signature.
7. Replay test (the research's test, on the Codex subscription): the public incidents and the Friday case expressed as authority objects run through the v1 rules; report decisions and the misses, so the gap between v1 rules and the compiled-rules increment is measured, not guessed.

## 11. Left to the builder

The SDK's exact FastAPI wiring, and the required-extension check in our own handler since `a2a-sdk` does not enforce `required` for us; the generalized Clerk verifier and the second OAuth application plus protected-resource metadata for `/a2a`; storage tables (DynamoDB, matching `messaging_repo`); the replay store; key management for the issuer key's `kid` rotation; the `ask_session` tool in the Hermes plugin; the exact app UI for the holder's Approve / Deny; error strings; rate limits on the public routes (reuse `_public_ingress` buckets for the card, Clerk for everything else).

## 12. Open questions for the founder

1. External clients: after the ponytail pass, phase 1 accepts any client holding a Lightbulb sign-in for the org; third-party OAuth clients are phase 1b. Confirm that is acceptable, or ask for the OAuth application in phase 1.
2. Answered 2 Nov: the workspace admin holds authority over a hosted agent.
3. Answered 2 Nov: publish the extension spec at launch; propose it to the A2A project later if we want.
