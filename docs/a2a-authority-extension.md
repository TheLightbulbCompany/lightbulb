# A2A authority extension

Extension URI: `https://thelightbulb.company/a2a/ext/authority/v1`

This page describes what Lightbulb's A2A endpoint does today. Source: `core/services/a2a_authority.py`, `a2a_auth.py`, `a2a_rules.py`, `a2a_tasks.py` and `routers/a2a.py` in `apps/backend`.

## What it is

An A2A extension that attaches a signed statement of authority to every task: who asked, through which agents, for whom, what operation on which resource with which exact operands, and in the person's own words. The endpoint decides each request with a deterministic rule document (no model), and signs a record of every decision.

Receiving a message grants nothing. Only a verified authority object, passed through the rules, does. Message text is an input to the decision and never changes the rules.

## The extension

- **Declared** in each Agent Card under `capabilities.extensions` with `required: true`. The card is signed (see Card signature) and has two security requirements, either of which authenticates a caller: a Lightbulb sign-in, or an OAuth grant (see OAuth clients and scopes). It also says `streaming: false` and lists two skills, `message` and `ask_session`. The door accepts only `message`; `ask_session` comes from a hosted agent through the internal route (any other skill at the door is `-32602 invalid authority: skill`).
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
| `requester` | `{person, org, verified_by}`: the person at the origin, their workspace, and how they were verified (`clerk-session` or `clerk-oauth` for the A2A door, `origin-message` or `agent-runtime` for a hosted agent's ask) |
| `chain` | Hops, origin first. Each hop is `{agent, session, person, at}`. The first hop's `person` equals `requester.person`. For a signed-in caller the first hop's `agent` is `null`; for an OAuth caller it is the OAuth client id. The backend writes the whole chain in one signature; there is no per-hop re-signing |
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

Authentication is a Lightbulb sign-in token or an OAuth grant (next section); the caller must belong to `{org}` (403 `wrong_workspace` otherwise). A grant missing the scope a method needs gets HTTP 403 `scope_required` before the method runs.

## OAuth clients and scopes

The door accepts two kinds of bearer token. A token that is a JWT is checked as a Lightbulb sign-in and gets both scopes. Any other token is checked as a Clerk OAuth grant for the resource `<API base>/a2a`. Per-agent keys are not accepted on this door.

| Scope | Allows |
|---|---|
| `lightbulb:a2a:send` | `SendMessage` |
| `lightbulb:a2a:read` | `GetTask` and `GET .../tasks/{task}/record` |

A grant must also carry `user:org:read` and an organization: a grant with no workspace (a free account) gets 403 `workspace_required`, and an invalid token gets 401 `invalid_token`. The decide and rules routes take a Lightbulb sign-in only.

Discovery: `GET /.well-known/oauth-protected-resource/a2a` names the resource, Clerk as the authorization server and the supported scopes. The Agent Card lists an `oauth` scheme (authorization code with PKCE, Clerk's authorize and token URLs) beside the `clerk` bearer scheme.

An OAuth caller is still a person: `requester.person` is the signed-in user who granted access, `requester.verified_by` is `clerk-oauth`, and `chain[0].agent` is the OAuth client id. Rules name the client as `agent:<client_id>` in `requesters`, and a `when` clause can require `verified_by` of `clerk-oauth`.

Operator step: the two scopes must be created in the Clerk Dashboard before a client can be granted them.

## The rule document and evaluation

One JSON document per workspace:

```
{ "revision": "<assigned by the server>",
  "holders": { "agent:*": ["user_..."], "session:*": ["@session-person"], "room:*": ["user_..."] },
  "rules": [ { "id": "r1", "resource": "agent:*", "skill": "message",
               "requesters": ["org:org_..."], "effect": "allow" } ],
  "default": "hold" }
```

`resource` matches exactly or by a trailing `*`. `requesters` entries are a person id, `org:<id>`, or `agent:<id>` (matches any agent in the chain, including an OAuth client id). `effect` is `allow`, `deny` or `hold`. `@session-person` stands for the person who owns the target laptop session. Holders are chosen by the exact key, else the longest wildcard prefix.

**`when` clauses.** A rule may carry `when`, extra conditions that must all hold for the rule to match; every clause that is set must match (AND). Field names are camelCase as the API returns them; snake_case is accepted on input only.

| Field | Type | Matches when |
|---|---|---|
| `operandsEqual` | object of strings | every key is present in the request's operands with exactly that value |
| `intentMatches` | string | the request's intent equals it after both are case-folded and whitespace is collapsed |
| `verifiedByIn` | list of strings | the requester's `verified_by` is one of them |

A clause with no input to test (no operands, no intent, no `verified_by`) does not match, so the rule does not apply. A `deny` rule whose `when` matches still wins over an exception; a `deny` rule whose `when` does not match is simply not a hit.

Evaluation, in this order:

1. Requester's org differs from the workspace: `deny`, rule `outside_org`.
2. Any matching `deny` rule wins, wherever it sits.
3. A stored exception for the same resource, skill, requester and `operands_hash`: `allow`, rule `exception:<id>`.
4. Otherwise the first matching rule in document order.
5. No match: `default`.
6. A `hold` whose requester is one of the holders, with a person at the origin: `allow`, rule `holder`. A person asking for a resource they hold needs nobody's OK. An ask that starts from an agent run with no person behind it stays held; its stand-in requester (the agent's first holder) may answer it.

With no stored rules document, the engine uses defaults: in-org messages to an agent are allowed (r1); session resources are held for the session's person, and the session's own person is allowed (`holder`); for agent and room resources the only holder is the calling admin, so an admin's own request that would be held is allowed as `holder` and a non-admin's has no holder and is rejected as `no_holder`; an autonomous ask (no origin person) has no requester and gets 403 `no_requester`. Store a rules document to name holders for agents and rooms.

A hold with no holder is refused with rule `no_holder`. An exception is created when a holder allows: one day, only for the same resource, skill, requester and operands hash. Another person's identical request is still held.

**Revision.** The server assigns `revision` on `PUT`: the hex SHA-256 of the document's canonical JSON (sorted keys, no whitespace) without `revision`. A client's `revision` is ignored; the same document always gets the same revision, and `GET` returns the stored one. The document is stored first, so a hold made from then on is decided by the new rules. Then every held task decided under an older revision is re-decided under the new document, one at a time:

- allow: the task is dispatched, record `allow_by_policy`. No exception is created.
- deny: the task is rejected, record `deny_by_policy`.
- hold: the task stays held with the new holders and revision, record `redecided`. Holders are asked again, with a fresh card, only when the holder set changed; the earlier cards still answer the task.
- hold with no holder: rejected as `no_holder`, record `deny_by_policy`.
- a hold already past 24 hours: canceled as `expired`.
- a task stored before this phase (without its decision inputs): canceled as `policy_changed`. New tasks never get this reason.

Every write expects the task to be held, so a holder who answered first keeps their answer. A holder's press on a task whose revision is older than the stored one re-decides it inline first, then answers only if the task is still held and that person still holds it. A re-decide that fails for one task is logged and does not stop the others; any later `PUT` re-decides a held task left under an older revision, so a retried `PUT` repairs it.

## Task states and the record

Task states (A2A names): `working` is `TASK_STATE_WORKING`; `held` is `TASK_STATE_AUTH_REQUIRED`; `completed`, `rejected`, `canceled` and `failed` map to their `TASK_STATE_` names. `Task.metadata[URI]` carries `state`, `rule`, the latest signed `record`, and `reason` when there is one.

- A hold expires after 24 hours: `canceled`, reason `expired`.
- A `working` task not finished 24 hours after dispatch becomes `failed`, reason `undeliverable`. Both expiries are noticed when the task is read (or, for `working`, when a reply arrives) and written once with a signed record; a reply after that completes nothing.
- A failed dispatch is `failed`, reason `dispatch_failed` (or `undeliverable` for a laptop session: no linked computer, or the send was refused).
- A new policy revision re-decides held tasks (see Revision); only a task stored before decision inputs were recorded is canceled as `policy_changed`.
- When a workspace agent replies to the dispatched message, the reply becomes the task's artifact and the task `completed`.
- An `ask_session` the laptop session accepted is `completed` at once, with artifact `{"artifactId": <request id>, "parts": [{"text": "delivered"}]}` and `delivered: true` in the metadata. The session's reply does not come back to the task in phase 1.

Each state change appends a signed record (JWS, `typ` `lightbulb-a2a-record`, same issuer key):

```
{ "task", "authority_id", "authority_hash", "decision", "rule", "policy_revision",
  "holder", "answered_by", "decided_at", "iat",
  "input": { "requester", "chain", "operation", "intent" } }
```

`decision` is one of `allow`, `deny`, `hold`, `allow_by_holder`, `deny_by_holder`, `allow_by_policy`, `deny_by_policy`, `redecided`, `completed`, `failed`, `canceled`. A rejection by rule or by `no_holder` is recorded as `deny`. `rule` is a rule id, `default`, `outside_org`, `exception:<id>`, `holder`, `no_holder`, or a reason (`expired`, `policy_changed` for rows stored before decision inputs were recorded, `dispatch_failed`, `undeliverable`). A re-decide records the rule that now applies. `input` is the snapshot the decision saw. A record is readable by the requester, the task's holders and workspace admins.

## Card signature

The Agent Card carries a JWS signature made with the issuer key that `GET /a2a/keys.json` serves. The protected header is `{"typ": "JOSE", "alg": "EdDSA", "kid": "<issuer key id>", "jku": "<API base>/a2a/keys.json"}`. To verify, a client takes the `kid` from the signature header, fetches the key with that `kid` from `keys.json`, and checks the card with the A2A SDK's signature verifier. When the backend has no issuer key configured, the card is served unsigned.

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

`decide` errors: 404 `not_found`; 403 `self` (the requester, unless the task's requester is an agent run's stand-in, `verified_by: agent-runtime`), 403 `not_holder`; 409 `expired`, 409 `not_held` (already answered, canceled, rejected or never held, including a row stored before decision inputs were recorded that a rules change canceled). A second answer to the same task loses.

**Asking the holder.** Each holder gets a direct message from the agent with the operation and resource, the requester and the chain (`person → agent` ids), the first 200 characters of the intent, the first 500 characters of the operand text (or the operands as compact JSON), and the `decide` URL. The message ends with an OpenUI card (a fenced block with language `openui-lang`) holding an Approve and a Deny button. The `decide` route stays available with a Lightbulb sign-in token.

**Answering from the DM.** Pressing a button sends a reply whose text is `Approve` or `Deny`. It counts as an answer only when all of these hold: it replies to the hold notification, it was sent by the holder that notification was addressed to, it is in that notification's room, and it came through the web person path (not a coding session's computer link). A recognized press records the decision (`allow_by_holder` or `deny_by_holder`) with reason `in-app`, and the press is stored quietly without waking the agent. If the decision is refused (already answered, expired, not held), the agent says in the room `could not record your answer: <error name>` (for example `not_held`); the press is still not passed to the agent. Any other message is an ordinary message.

An allowed `message` is posted to the agent in the requester's direct-message room and the task is `working` until the agent's final reply, addressed to the dispatched message, arrives. An `ask_session` is sent as `app.send` through the origin person's most recently connected computer link, as `<agent name> via <person>`, with a first line naming who asked, for whom, why, and the record URL; see the task states above for what the task shows.

## What is not built

- Streaming (`SendStreamingMessage`) and the REST binding; JSON-RPC only.
- `CancelTask` (returns `-32004`).
- Answering a hold outside the DM: the Approve and Deny card lives in the holder's direct message, and the `decide` route takes a Lightbulb sign-in only, not an OAuth grant.
- A laptop session's reply flowing back to its `ask_session` task; the task ends at `completed` (delivered).
- Chain re-signing and narrowing checks across hops; the backend signs the whole chain once.
- Autonomous asks (no person behind the turn) need a stored rules document, because the stand-in requester is the agent's first holder and none exists without one.
- A reply that is not addressed to the dispatched message (no `replyToMessageId`, or another one) links to no task. The hosted bridge addresses its final reply to the message that triggered the turn.

## How to run the replay

From `apps/backend`: `CLERK_ISSUER=<any issuer url> uv run python -m scripts.a2a_replay`. Importing `core.services` loads Settings, which requires `CLERK_ISSUER`; the test suite sets it.
