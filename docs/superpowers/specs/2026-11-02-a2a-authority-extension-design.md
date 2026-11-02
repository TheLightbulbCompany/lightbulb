# A2A as the protocol, authority as a required extension: design

**Status:** draft for the founder's approval. Nothing is built.
**Decision this implements (2 Nov 2026):** governance is built incrementally, planned first, tested, coordinated with the Workers session, and the first increment is A2A. A2A (Linux Foundation, v1.0) becomes the network's protocol; our peer-to-peer transport becomes an A2A binding later; the authority object is a *required* A2A extension; the hosted workspace member speaks A2A over HTTP first.
**Research it rests on:** `docs/research/2026-11-01-org-auto-mode-governance.md` and its strands 01-11 (PR #4551).

## 1. Intent

An agent that acts for one person and asks another person's agent or computer to do something must carry, on every hop, a verifiable statement of who asked, through which agents, for whom, and exactly what. A point the company controls decides whether that request is allowed, by rules the company wrote, and records which rule decided. Out-of-rule requests go to the person who holds authority over the resource, not to the agent's owner. People see escalations and the record, never per-call prompts.

Why A2A: it is the interop protocol the deployed-agent vendors are converging on, it has a formal extension mechanism designed for things like message signing, and it explicitly leaves authorization to the implementation (spec §13.1: "Authorization boundaries are defined by each agent's authorization model, not prescribed by the protocol"). It has no delegation or on-behalf-of mechanism. That hole is this product.

## 2. Goals, non-goals, anti-goals

Goals (this increment):
- Any A2A client can send a task to a hosted workspace agent (Friday) at a Lightbulb URL, and the request is refused unless it carries our authority extension.
- A hosted agent's request to a person's laptop session carries the same object, is decided by the workspace's rules, and is held for the session's person when the rules do not decide.
- Every decision is recorded, signed, and retrievable with the task.
- An external party can verify the record with our published key.

Non-goals (later increments): migrating the P2P network's frames to the A2A data model (phase 2); compiling company sentences into rules with an LLM; spend mandates; adapters for Slack, Notion, Copilot Studio; a classifier of any kind.

Anti-goals (a failure even if it works): a dialog to the agent's owner for every request; an approvals inbox as the product; any path where the extension can be omitted and the request still runs; an unsigned record; a bespoke protocol that only Lightbulb speaks.

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
**Carried** in `Message.metadata` under the key equal to the URI. Spec: extensions put custom attributes in `metadata`, keyed by URI; they must not add fields to core structures.

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

Who signs: the minter of the hop. At the hosted door the backend mints and signs after Clerk verification (the client's token proves the person and org). A laptop daemon will sign with its computer key in phase 2. Each later hop appends to `chain` and re-signs the whole object; the previous signature is kept as `prev_sig` so the chain is verifiable end to end. Authority can only narrow per hop: a hop may not add skills or widen operands.

Verification at the target, in order: signature by a known key; `exp`; `id` unseen (replay store, 24h); `chain[0].person == requester.person`; `operands_hash` matches the operands the target is about to act on; the Message's `extensions` array lists the URI. Any failure rejects the task with A2A error `-32005` (unsupported or invalid extension) and a status message naming the check, never the secret.

The spec's security rules apply: extension data is untrusted input, validated rigorously; the extension "MUST NOT provide a way to bypass the agent's primary security controls" (it is the control, applied after authentication, never instead of it).

## 5. The endpoint (hosted, FastAPI)

New router `apps/backend/routers/a2a.py`, included in `main.py` before the MCP catch-all mount at `main.py:1123`. Python `a2a-sdk` with the FastAPI integration for the JSON-RPC and REST bindings; our code is the request handler, the extension check, the decision, and the record.

Routes (my call; the builder may regroup):
- `GET /a2a/{org}/{agent}/agent-card.json`: the signed Agent Card. `supportedInterfaces[0] = { url: ".../a2a/{org}/{agent}", protocolBinding: "JSONRPC", protocolVersion: "1.0", tenant: "{org}" }`. `securitySchemes`: OAuth2 bearer via Clerk (the hosted MCP's verifier, `hosted_mcp.py:37`, with audience `{PUBLIC_API_BASE}/a2a`). `capabilities.extensions = [ {uri, required: true, description} ]`. `skills`: `message` (send the agent a message and get its reply), `ask_session` (ask one of the workspace's laptop sessions; v1 exposes this only to workspace agents, not external clients). `signatures`: JWS per §8.4 with `kid` = the issuer key id, canonicalized per RFC 8785.
- `POST /a2a/{org}/{agent}`: JSON-RPC `message/send`, `message/stream`, `tasks/get`, `tasks/cancel`. REST equivalents from the SDK.
- `GET /a2a/{org}/{agent}/tasks/{id}/record`: the signed decision record (also embedded in `Task.metadata` under the extension URI).
- `POST /a2a/{org}/{agent}/tasks/{id}/decide`: the holder's answer to a held task (allow or deny, with a reason). Only the holder's verified identity may call it; the requester may never answer their own request.
- `GET /a2a/keys.json`: the public keys (JWKS) that sign cards and records.

Task lifecycle:
1. Authenticate (Clerk bearer, org membership) or reject `401/403`.
2. Require the extension (header or `Message.extensions`) or reject per §4.6.3.
3. Verify the object (§4). The backend also mints and signs the object for clients that authenticated but sent none? **No.** A required extension is required; the client must send it. Lightbulb's own clients (the app, the CLI, Friday) always send one.
4. Decide (§6): `allow` → task `TASK_STATE_WORKING`, dispatch to the agent by today's path (`messaging.create_human_message` → `openclaw_work._dispatch_message` → Hermes `/plugins/lightbulb-channel/inbound/{agentId}`), reply becomes an Artifact, task `COMPLETED`. `deny` → `TASK_STATE_REJECTED` with the rule named. `hold` → `TASK_STATE_AUTH_REQUIRED` with a status message "waiting for <holder display name>" and `Task.metadata[uri].state = "held"`; the holder is notified (§7); on allow the task continues, on deny it is `REJECTED`, on expiry (24h) it is `CANCELED`.
5. Record (§6) at every state change.

Spec note on the held state: §7.6 defines `TASK_STATE_AUTH_REQUIRED` for an agent that needs the client to supply authorization, and §7.6.4 says the transition is never itself an authorization. We use it for "a person who holds the resource must authorize", which is the same shape; the substate rides in metadata as a profile extension allows. My call; a reviewer may prefer `INPUT_REQUIRED`.

## 6. The decision and the record

Rules v1 are a JSON document per workspace, stored beside the company policy map (`billing_repo.py:1446` holds `CAPABILITY_POLICY_KEYS`; that map stays unenforced and untouched):

```
{
  "revision": "<ulid>",
  "holders": {
    "agent:friday": ["user_<workspace admin>"],
    "session:*": "its person",
    "room:*": ["user_<workspace admin>"]
  },
  "rules": [
    { "id": "r1", "resource": "agent:friday", "skill": "message",
      "requesters": ["org:org_..."], "effect": "allow" },
    { "id": "r2", "resource": "session:*", "skill": "ask_session",
      "requesters": ["agent:friday"], "effect": "hold" },
    { "id": "r3", "resource": "session:*", "skill": "ask_session",
      "requesters": ["agent:friday"], "when": {"intent_matches": "status of my own session"}, "effect": "allow" }
  ],
  "default": "hold"
}
```

Evaluation is deterministic: the first rule whose resource, skill, requester and `when` match wins; `deny` rules are evaluated before `allow` rules at the same specificity (Cedar's forbid-overrides-permit); no match → `default`. Requesters outside the org → `deny` always. `when` in v1 supports only exact operand equality and a literal `intent_matches` string; no model is consulted. `on_behalf_of` ≠ requester → `deny` in v1.

A held task's answer becomes a time-boxed exception row: `{rule: "exception", holder, task, operands_hash, expires}`; a later identical request within the window is allowed by that row and recorded as such. Exceptions never widen beyond the operands hash.

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

The holder is looked up from `holders`. For `session:*` it is the session's person as the backend knows it (`local_sessions`). Notification in v1: a direct message to the holder in the app with the intent, the operation and operands, the requester chain, and Approve / Deny, which call `/decide` with the holder's own Clerk session. No self-approval: if holder == requester, the task is `REJECTED` with rule `self`. The agent's owner is informed by a message, not asked. A native dialog on the holder's Mac (the shape CLI PR #115 used for `team.join`) is phase 2, when the daemon signs its own hops.

## 8. Dispatch to a laptop session

v1 uses the path that exists: `native_session_tools.session_send` → `local_sessions.submit_command` → `local_session_relay.dispatch_session` → the Mac app (`native_session_tools.py:417`, `local_session_relay.py:1389`). The signed authority object travels in the command body and is rendered into the session's framed message as a short header (requester, for whom, intent) plus a link to the record. The session does not verify it in v1; the backend did. Phase 2 moves this to the P2P network with the daemon verifying.

## 9. Phase 2 outline (not this increment, written so phase 1 does not paint us in)

- `Frame::Envelope` gains an A2A `Message` body: `messageId`, `role`, `parts`, `contextId`, `taskId`, `metadata`, `extensions`. Old `text` maps to one `TextPart`. Envelopes get a `sig` by the computer key (the ponytail note at `frames.rs:94` anticipates it). Unknown-variant tolerance is added before the change ships, one version ahead.
- The daemon serves and consumes Agent Cards over the network (a card per member, signed with the computer key, distributed with the roster) and declares the P2P binding in the card per §12.
- Holds `Kind::Message` is revived as the local `AUTH_REQUIRED` state; the signed `approvals` record (`holds.rs:244`) becomes the record for laptop-side decisions, with the same fields as §6.
- Workers: `assign` becomes a task with the authority object; `Policy { Allow, Ask, Refuse }` rows become rules in the workspace document.
- The cloud member's bridge (`app.send` via `Caller::Instance`) is the hosted A2A server's path into the network, replacing §8.

## 10. Tests (phase 1 acceptance)

1. Conformance: the official `a2a-sdk` Python client fetches the card, sends `message/send` with the extension, and gets a `COMPLETED` task with Friday's reply; the same without the extension gets the required-extension error.
2. Tampering: an object with a changed operand, an expired `exp`, a replayed `id`, a chain whose origin was edited, and a bad signature each produce a rejection naming the check.
3. Hold: Friday asks a laptop session something not covered by a rule; the task goes `AUTH_REQUIRED`; the holder approves in the app; the task completes; the record shows `allow_by_holder` with the holder's id; the requester cannot call `/decide`.
4. Self-approval: holder == requester → `REJECTED`, rule `self`.
5. Exception window: a second identical request within the window is `allow` by `exception:<id>`; a request with different operands is held again.
6. Record verification: a script outside the backend fetches `/a2a/keys.json` and verifies a record's JWS and the card's signature.
7. Replay test (the research's test, on the Codex subscription): the public incidents and the Friday case expressed as authority objects run through the v1 rules; report decisions and the misses, so the gap between v1 rules and the compiled-rules increment is measured, not guessed.

## 11. Left to the builder

The SDK's exact FastAPI wiring; storage tables (DynamoDB, matching `messaging_repo`); the replay store; key management for the issuer key's `kid` rotation; the exact app UI for the holder's Approve / Deny; error strings; rate limits on the public routes (reuse `_public_ingress` buckets for the card, Clerk for everything else).

## 12. Open questions for the founder

1. External clients in v1: any A2A client in the workspace's org (recommended), or only Lightbulb's own apps until phase 2?
2. The holder of a hosted agent like Friday: the workspace admin (recommended), or the person who created the agent?
3. Publishing: do we publish the extension spec on thelightbulb.company at launch and propose it to the A2A project, or keep it private until a design partner uses it?
