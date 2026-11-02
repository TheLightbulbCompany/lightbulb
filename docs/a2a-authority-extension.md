# A2A authority extension

Extension URI: `https://thelightbulb.company/a2a/ext/authority/v1`

This page describes what Lightbulb's A2A endpoint does today (phase 1). Source: `core/services/a2a_authority.py`, `a2a_rules.py`, `a2a_tasks.py` and `routers/a2a.py` in `apps/backend`.

## What it is

An A2A extension that attaches a signed statement of authority to every task: who asked, through which agents, for whom, what operation on which resource with which exact operands, and in the person's own words. The endpoint decides each request with a deterministic rule document (no model), and signs a record of every decision.

Receiving a message grants nothing. Only a verified authority object, passed through the rules, does. Message text is an input to the decision and never changes the rules.

## The extension

- **Declared** in each Agent Card under `capabilities.extensions` with `required: true`. The card also says `streaming: false` and lists two skills, `message` and `ask_session`. The door accepts only `message`; `ask_session` comes from a hosted agent through the internal route (any other skill at the door is `-32602 invalid authority: skill`).
- **Activated** by listing the URI in the message's `extensions` array. A message without it is rejected with `-32008`.
- **Carried** in `Message.metadata`, under the key equal to the URI, as an unsigned authority request:

```
{ "operation": { "skill": "message", "resource": "agent:<id>", "operands": { "text": "..." } },
  "intent": "<the person's words, up to 2048 characters>",
  "on_behalf_of": null }
```

`skill` must be `message`, and `operands` must equal `{"text": <the message's text parts joined by newlines>}`. The signed operands are exactly what the agent receives. `resource` must be `agent:<id>` of the card's agent.

## The authority object

The endpoint turns the request into this object and signs it. A client never supplies a signed object in phase 1.

| Field | Meaning |
|---|---|
| `v` | `1` |
| `id` | UUID, one per request; a second use is rejected as a replay |
| `iat`, `exp` | Unix seconds; `exp - iat` is at most 86400 |
| `requester` | `{person, org, verified_by}`: the person at the origin, their workspace, and how they were verified (`clerk-session` for the A2A door, `origin-message` or `agent-runtime` for a hosted agent's ask) |
| `chain` | Hops, origin first. Each hop is `{agent, session, person, at}`. The first hop's `person` equals `requester.person`. The backend writes the whole chain in one signature; there is no per-hop re-signing |
| `on_behalf_of` | `null` or the requester. Anyone else is invalid |
| `operation` | `{skill, resource, operands, operands_hash}`. `operands_hash` is `sha256:` plus the hex digest of the operands as JSON with sorted keys, no whitespace, UTF-8. Operands that cannot be encoded (a lone surrogate) fail as `operands` |
| `intent` | The person's words, at most 2048 characters |
| `policy` | `null` in phase 1 |

**Signing.** A compact JWS, EdDSA, header `typ` `lightbulb-a2a-authority`, signed with the same issuer key that signs Lightbulb sign-in proofs. Public keys are at `GET /a2a/keys.json`, a JWKS: `{"keys": [{"kty": "OKP", "crv": "Ed25519", "x": "<base64url raw public key>", "alg": "EdDSA", "use": "sig", "kid": "..."}]}`. **Who mints.** Only the Lightbulb backend, from the authenticated caller. `requester` and `chain[0]` come from the verified identity, never from the request.

## Verification

Checked in this order; the first failure names the check:

1. `signature`: valid EdDSA signature by the issuer key, matching `kid`
2. `typ`: `lightbulb-a2a-authority` for an object, `lightbulb-a2a-record` for a record
3. `shape`: `iat` is an integer
4. `iat`: not in the future
5. `exp`: present, not passed, at most 24 hours after `iat`
6. `shape`: required fields present with the right types
7. `intent_length`: at most 2048 characters
8. `on_behalf_of`: null or the requester
9. `origin`: `chain` is non-empty and its first hop is the requester
10. `operands_hash`: matches the operands

Errors on `POST /a2a/{org}/{agent}` are JSON-RPC errors:

| Code | When |
|---|---|
| `-32008` | extension not listed in `Message.extensions`, or no authority request under the URI key |
| `-32602` | invalid params: `invalid authority: shape`, `skill`, `resource`, `operands`, or a failed check above; also `replay`, an unknown agent, a message with no text part, or `params` not an object |
| `-32004` | `CancelTask` (unsupported) |
| `-32601` | unknown method |
| `-32602` | `GetTask` with `params.id` missing or not a string |
| `-32001` | `GetTask` for a task that does not exist or that the caller may not read |

Authentication is a Lightbulb sign-in (Lightbulb sign-in token); the caller must belong to `{org}` (403 `wrong_workspace` otherwise).

## The rule document and evaluation

One JSON document per workspace:

```
{ "revision": "<assigned by the server>",
  "holders": { "agent:*": ["user_..."], "session:*": ["@session-person"], "room:*": ["user_..."] },
  "rules": [ { "id": "r1", "resource": "agent:*", "skill": "message",
               "requesters": ["org:org_..."], "effect": "allow" } ],
  "default": "hold" }
```

`resource` matches exactly or by a trailing `*`. `requesters` entries are a person id, `org:<id>`, or `agent:<id>` (matches any agent in the chain). `effect` is `allow`, `deny` or `hold`. `@session-person` stands for the person who owns the target laptop session. Holders are chosen by the exact key, else the longest wildcard prefix.

Evaluation, in this order:

1. Requester's org differs from the workspace: `deny`, rule `outside_org`.
2. Any matching `deny` rule wins, wherever it sits.
3. A stored exception for the same resource, skill, requester and `operands_hash`: `allow`, rule `exception:<id>`.
4. Otherwise the first matching rule in document order.
5. No match: `default`.
6. A `hold` whose requester is one of the holders, with a person at the origin: `allow`, rule `holder`. A person asking for a resource they hold needs nobody's OK. An ask that starts from an agent run with no person behind it stays held; its stand-in requester (the agent's first holder) may answer it.

With no stored rules document, the engine uses defaults: in-org messages to an agent are allowed (r1); session resources are held for the session's person, and the session's own person is allowed (`holder`); for agent and room resources the only holder is the calling admin, so an admin's own request that would be held is allowed as `holder` and a non-admin's has no holder and is rejected as `no_holder`; an autonomous ask (no origin person) has no requester and gets 403 `no_requester`. Store a rules document to name holders for agents and rooms.

A hold with no holder is refused with rule `no_holder`. An exception is created when a holder allows: one day, only for the same resource, skill, requester and operands hash. Another person's identical request is still held.

**Revision.** The server assigns `revision` on `PUT`: the hex SHA-256 of the document's canonical JSON (sorted keys, no whitespace) without `revision`. A client's `revision` is ignored; the same document always gets the same revision, and `GET` returns the stored one. A new revision first cancels every held task with reason `policy_changed`, then stores the document. Held tasks are not re-decided. A hold that had already expired cancels as `expired`. A holder's answer to a task decided under an older revision than the stored one (a hold that raced the store) cancels it with `policy_changed` and returns 409 `policy_changed`.

## Task states and the record

Task states (A2A names): `working` is `TASK_STATE_WORKING`; `held` is `TASK_STATE_AUTH_REQUIRED`; `completed`, `rejected`, `canceled` and `failed` map to their `TASK_STATE_` names. `Task.metadata[URI]` carries `state`, `rule`, the latest signed `record`, and `reason` when there is one.

- A hold expires after 24 hours: `canceled`, reason `expired`.
- A `working` task not finished 24 hours after dispatch becomes `failed`, reason `undeliverable`. Both expiries are noticed when the task is read (or, for `working`, when a reply arrives) and written once with a signed record; a reply after that completes nothing.
- A failed dispatch is `failed`, reason `dispatch_failed` (or `undeliverable` for a laptop session: no linked computer, or the send was refused).
- A new policy revision cancels held tasks with reason `policy_changed`.
- When a workspace agent replies to the dispatched message, the reply becomes the task's artifact and the task `completed`.
- An `ask_session` the laptop session accepted is `completed` at once, with artifact `{"artifactId": <request id>, "parts": [{"text": "delivered"}]}` and `delivered: true` in the metadata. The session's reply does not come back to the task in phase 1.

Each state change appends a signed record (JWS, `typ` `lightbulb-a2a-record`, same issuer key):

```
{ "task", "authority_id", "authority_hash", "decision", "rule", "policy_revision",
  "holder", "answered_by", "decided_at", "iat",
  "input": { "requester", "chain", "operation", "intent" } }
```

`decision` is one of `allow`, `deny`, `hold`, `allow_by_holder`, `deny_by_holder`, `completed`, `failed`, `canceled`. A rejection by rule or by `no_holder` is recorded as `deny`. `rule` is a rule id, `default`, `outside_org`, `exception:<id>`, `holder`, `no_holder`, or a reason (`expired`, `policy_changed`, `dispatch_failed`, `undeliverable`). `input` is the snapshot the decision saw. A record is readable by the requester, the task's holders and workspace admins.

## Routes

| Route | Use |
|---|---|
| `GET /a2a/keys.json` | public issuer keys (JWKS) |
| `GET /a2a/{org}/{agent}/agent-card.json` | Agent Card (public, rate limited) |
| `POST /a2a/{org}/{agent}` | JSON-RPC: `SendMessage` (returns the task under `task`, does not block), `GetTask` |
| `GET /a2a/{org}/{agent}/tasks/{task}/record` | every signed record for the task, oldest first; 404 `task_not_found` when the task is missing or unreadable |
| `POST /a2a/{org}/{agent}/tasks/{task}/decide` | a holder answers a held task: `{"verdict": "allow" or "deny", "reason": ""}` |
| `GET`, `PUT /a2a/{org}/rules` | read or replace the rule document (workspace admins); `PUT` returns the stored document with its server revision |
| `POST /api/v1/internal/a2a/{owner}/ask-session` | a hosted agent asks a laptop session; authenticated by the runtime's hook token, not by users |

`decide` errors: 404 `not_found`; 403 `self` (the requester, unless the task's requester is an agent run's stand-in, `verified_by: agent-runtime`), 403 `not_holder`; 409 `expired`, 409 `not_held` (already answered, canceled or never held), 409 `policy_changed`. A second answer to the same task loses.

**Asking the holder.** Each holder gets a direct message from the agent with the operation and resource, the requester and the chain (`person → agent` ids), the first 200 characters of the intent, the first 500 characters of the operand text (or the operands as compact JSON), and the `decide` URL. The holder answers by calling that route with their own Lightbulb sign-in token; an Approve or Deny button in the app is phase 1b.

An allowed `message` is posted to the agent in the requester's direct-message room and the task is `working` until the agent's final reply, addressed to the dispatched message, arrives. An `ask_session` is sent as `app.send` through the origin person's most recently connected computer link, as `<agent name> via <person>`, with a first line naming who asked, for whom, why, and the record URL; see the task states above for what the task shows.

## What phase 1 does not do

- Third-party OAuth clients: callers need a Lightbulb sign-in for the workspace.
- Streaming (`SendStreamingMessage`) and the REST binding; JSON-RPC only.
- `CancelTask` (returns `-32004`).
- Agent Card signatures.
- `when` clauses in rules (such as matching on intent or operands). Rules are resource, skill, requesters and effect.
- Re-deciding a held task when the revision changes; it is canceled.
- An Approve or Deny button in the app (phase 1b); a holder is asked by direct message and answers at the `decide` route with a Lightbulb sign-in token.
- A laptop session's reply flowing back to its `ask_session` task; the task ends at `completed` (delivered).
- Chain re-signing and narrowing checks across hops; the backend signs the whole chain once.
- Autonomous asks (no person behind the turn) need a stored rules document, because the stand-in requester is the agent's first holder and none exists without one.
- A reply that is not addressed to the dispatched message (no `replyToMessageId`, or another one) links to no task. The hosted bridge addresses its final reply to the message that triggered the turn.

## How to run the replay

From `apps/backend`: `CLERK_ISSUER=<any issuer url> uv run python -m scripts.a2a_replay`. Importing `core.services` loads Settings, which requires `CLERK_ISSUER`; the test suite sets it.
