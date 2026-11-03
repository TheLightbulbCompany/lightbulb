# A2A Authority Extension, Phase 1b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Outside A2A clients reach the door through Clerk OAuth, a holder answers a hold by pressing a card in their DM, rules can condition on operands and intent, a rules change re-decides held tasks instead of canceling them, and the Agent Card is signed.

**Architecture:** Five additive changes on the phase-1 backend: a second Clerk verifier instance and a caller dependency that accepts either a session JWT or an opaque OAuth token; a reply hook on the web message-post path that turns a holder's `Approve`/`Deny` press into `a2a_tasks.decide`; a `when` clause on rules evaluated inside `a2a_rules.decide`; a shared `_redecide` used by `put_rules` and by `decide`'s revision-mismatch path; and the SDK's card signer wired to the issuer key. Frontend changes are one test; the card is an OpenUI fence the frontend already renders.

**Tech Stack:** Python 3.12, FastAPI, `a2a-sdk` (plus its `signing` extra, which is PyJWT, already present), `cryptography`, DynamoDB via moto in tests, pytest; vitest for the one frontend test.

**Spec:** `docs/superpowers/specs/2026-11-02-a2a-authority-phase1b-design.md` (approved 2 Nov 2026).

## Global Constraints

- No new dependency beyond `a2a-sdk[signing]` (`uv add "a2a-sdk[signing]"` from `apps/backend`).
- Phase-1 invariants hold: the door mints and signs; deny always wins; every transition is a conditional write; records on every state change; the requester never answers their own hold (except agent-runtime origins, phase-1 ruling).
- A2A audience `RESOURCE_A2A = f"{settings.PUBLIC_API_BASE.rstrip('/')}/a2a"`; scopes `lightbulb:a2a:send` (SendMessage), `lightbulb:a2a:read` (GetTask, record route), plus `user:org:read`. Free accounts (`org_id` None) → 403 `workspace_required`. Per-agent keys are not accepted on the A2A door. Clerk Dashboard scope creation is the founder's step; the session-JWT path stays accepted.
- OAuth callers: `chain[0].agent = client_id`, `verified_by = "clerk-oauth"`; rules match them with the existing `agent:<client_id>` requester spelling.
- The hold notification carries an OpenUI fence: language tag `openui-lang`, program `root = Stack([b])` / `b = Buttons([Button("Approve", "primary"), Button("Deny", "secondary")])`; the press posts the trimmed label as a human reply with `replyToMessageId` = the notification message id and a mention of the agent (`packages/shared/src/ui/reply.ts`).
- Only the web person-JWT post path (`routers/messaging.py:985`, `label is None`) may answer a hold; a recognized answer is persisted with `wake=False`; the task is found by the notification message id; a failed decision never raises into the message path.
- `When` fields: `operands_equal: dict[str, str] | None`, `intent_matches: str | None`, `verified_by_in: list[str] | None`; `intent_matches` compares after case-folding and collapsing whitespace; a rule with `when` never matches when `decide` lacks the inputs.
- Re-decide outcomes: `allow` → dispatch, record `allow_by_policy`, no exception row; `deny` → `rejected`, record `deny_by_policy`; `hold` → stays held, `holders` and `policyRevision` updated, record `redecided`, holders re-notified only if the holder set changed; `hold` with no holders → `rejected` `no_holder`; already-expired holds cancel as `expired`; rows lacking the stored inputs cancel with `policy_changed`. `put_rules` stores the document FIRST, then re-decides.
- Card signature: `a2a.utils.signing.create_agent_card_signer` with a `PyJWK` built from the issuer's Ed25519 key, protected header `{kid: network_proof.kid(), alg: "EdDSA", jku: f"{PUBLIC_API_BASE}/a2a/keys.json", typ: "JOSE"}`; verifiable with `create_signature_verifier` against `keys.json`.
- Tests from `apps/backend`: `uv run pytest <files> -v --no-cov`; frontend from `apps/frontend`: `npx vitest run src/components/messaging/__tests__/openuiAsk.test.tsx`. Draft PR, no merge, no release, never print secrets, no production data.

## Review Focus

1. An OAuth token whose `aud` holds only the `/mcp` resource presented to the A2A door: expected 401, never accepted on scope alone unless the Clerk-audience fallback is explicitly enabled. Pinned in Task 1.
2. A holder's ordinary reply in the DM whose `reply_to_message_id` is not a hold notification: expected a normal message, agent woken, no decision. Pinned in Task 2.
3. A `when.operands_equal` naming a key the request's operands lack: expected the rule does not match (no exception, no crash). Pinned in Task 3.
4. A re-decide that turns a held task into `allow` whose dispatch then fails: expected `failed` with a record, never a stuck hold. Pinned in Task 4.
5. The card served when `NETWORK_PROOF_KEY` is unset: expected an unsigned card and a logged warning, not a 500. Pinned in Task 5.

---

### Task 1: OAuth clients on the A2A door

**Files:**
- Modify: `apps/backend/core/services/hosted_mcp.py:57-130` (`ClerkOAuthVerifier` gains constructor parameters `resource: str`, `required_scopes: set[str]`; module constants keep their values by constructing the MCP instance with them)
- Create: `apps/backend/core/services/a2a_auth.py`
- Modify: `apps/backend/main.py` (metadata route beside the `/mcp` one at `main.py:1180`; `RESOURCE_A2A` import)
- Modify: `apps/backend/routers/a2a.py` (the JSON-RPC route and the record route use the new dependency; scope gating)
- Modify: `apps/backend/core/services/a2a_tasks.py` (`send_message` reads `verified_by` and the first-hop agent from the caller context; `agent_card` security schemes)
- Test: `apps/backend/tests/unit/services/test_a2a_auth.py`, `apps/backend/tests/unit/routers/test_a2a_oauth.py`

**Interfaces:**
- Consumes: `ClerkOAuthVerifier.verify_token(token) -> AccessToken | None` (claims include `org_id`, `client_id`), `core.auth.get_current_user`, `AuthContext`.
- Produces:
  - `hosted_mcp.ClerkOAuthVerifier.__init__(self, *, resource: str = RESOURCE, required_scopes: set[str] | None = None)`; behaviour unchanged for the MCP instance.
  - `a2a_auth.RESOURCE_A2A: str`, `SEND_SCOPE = "lightbulb:a2a:send"`, `READ_SCOPE_A2A = "lightbulb:a2a:read"`
  - `@dataclass class A2ACaller`: `auth: AuthContext`, `verified_by: Literal["clerk-session", "clerk-oauth"]`, `client_id: str | None`, `scopes: frozenset[str]`
  - `async def a2a_caller(request: Request, credentials = Depends(security)) -> A2ACaller`: a token containing `.` → `get_current_user` path (`verified_by="clerk-session"`, scopes = `{SEND_SCOPE, READ_SCOPE_A2A}`); else the A2A verifier (`verified_by="clerk-oauth"`, `client_id` from claims, scopes from the token); `org_id` None → 403 `workspace_required`; unknown token → 401.
  - `def require_scope(caller: A2ACaller, scope: str) -> None` → 403 `scope_required`.
  - `a2a_tasks.send_message(caller: A2ACaller, org, agent_id, message, *, now)` (signature change: takes the caller; `verified_by` and `chain[0].agent` from it); existing tests updated to pass a session caller.
  - `agent_card` lists two `securityRequirements` entries (OR): `clerk` bearer and an `oauth2` scheme with `authorizationUrl`, `tokenUrl` read from `{CLERK_ISSUER}/.well-known/oauth-authorization-server` at first use (cached in-process; fallback `/oauth/authorize` and `/oauth/token` when the fetch fails) and the two scopes.
  - `GET /.well-known/oauth-protected-resource/a2a` → `{"resource": RESOURCE_A2A, "authorization_servers": [settings.CLERK_ISSUER], "scopes_supported": ["user:org:read", READ_SCOPE_A2A, SEND_SCOPE], "bearer_methods_supported": ["header"]}`.
  - Audience: the verifier requires `RESOURCE_A2A in aud`. Builder item: verify against the dev instance with one real CIMD token; if Clerk does not echo the requested resource, set `settings.A2A_ACCEPT_MCP_AUDIENCE = True` (new setting, default False) which makes the verifier accept the `/mcp` audience when the A2A scopes are present, and report which was needed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/services/test_a2a_auth.py  (patch ClerkOAuthVerifier.verify_token with AsyncMock returning an AccessToken-like object)
async def test_session_jwt_goes_to_the_session_path(monkeypatch): ...   # token "a.b.c" → verified_by clerk-session, scopes both
async def test_opaque_token_with_a2a_audience_and_send_scope_is_oauth_caller(): ...  # verified_by clerk-oauth, client_id "cimd:https://example/client.json"
async def test_mcp_audience_only_is_refused_unless_fallback_enabled(monkeypatch): ...  # 401; with settings.A2A_ACCEPT_MCP_AUDIENCE True and a2a scopes → accepted
async def test_free_account_is_403_workspace_required(): ...
async def test_require_scope_refuses_read_only_for_send(): ...
# tests/unit/routers/test_a2a_oauth.py (client fixture, override a2a_caller)
def test_oauth_send_mints_with_client_as_first_hop_and_verified_by_clerk_oauth(...): ...  # GetTask metadata record input chain[0].agent == client_id
def test_read_only_token_can_get_task_but_not_send(...): ...   # -32602? no: HTTP 403 scope_required for SendMessage; GetTask ok
def test_card_lists_two_security_requirements_and_oauth_urls(...): ...
def test_protected_resource_metadata_a2a(client): ...
```

- [ ] **Step 2: Run tests to verify they fail** — `uv run pytest tests/unit/services/test_a2a_auth.py tests/unit/routers/test_a2a_oauth.py -v --no-cov`, expected `ModuleNotFoundError: core.services.a2a_auth`.
- [ ] **Step 3: Implement** the verifier parameters, `a2a_auth.py`, the metadata route, the dependency on both routes, the `send_message` signature change (update `tests/unit/routers/test_a2a.py` and `test_a2a_hold.py` fixtures to build a session `A2ACaller`), and the card schemes.
- [ ] **Step 4: Run** the two new files plus `tests/unit/routers/test_a2a.py tests/unit/routers/test_a2a_hold.py tests/integration/test_a2a_conformance.py` — expected PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(a2a): OAuth clients on the door via a second Clerk audience; caller dependency; signed-in path kept"`

---

### Task 2: Approve/Deny from the DM

**Files:**
- Modify: `apps/backend/core/services/a2a_tasks.py` (`_notify_holders` text gains the fence; stores `notificationMessageIds`; new `answer_from_message`)
- Modify: `apps/backend/core/repositories/a2a_repo.py` (`put_notification(org, message_id, task_id, holder)` → `sk=NOTIF#{message_id}`; `get_task_by_notification(org, message_id) -> tuple[task_id, holder] | None`; `update_task` extra for `notificationMessageIds`)
- Modify: `apps/backend/routers/messaging.py:985-990` (web path only: call the hook before `create_human_message`, pass `wake=False` when it handled the reply)
- Test: `apps/backend/tests/unit/routers/test_a2a_card_answer.py`; frontend `apps/frontend/src/components/messaging/__tests__/openuiAsk.test.tsx` (one added test)

**Interfaces:**
- Consumes: `messaging.create_agent_message` receipt `.message_id`; `create_human_message(..., wake=...)`; `CreateMessageRequest.reply_to_message_id`, `.text`; `a2a_tasks.decide(auth, org, agent_id, task_id, *, verdict, reason, now)`; `resolve_owner_id(auth)` for the org.
- Produces:
  - Notification text ends with the fence (exact program in Global Constraints) after the phase-1 lines; `row["notificationMessageIds"] = {holder: message_id}` and a `NOTIF#` index row per holder.
  - `async def answer_from_message(auth: AuthContext, org: str, request: CreateMessageRequest, *, now: int | None = None) -> bool`: returns False unless `request.reply_to_message_id` indexes a hold notification whose `holder == auth.user_id` and `request.text.strip() in ("Approve", "Deny")`; then calls `decide(..., verdict="allow"|"deny", reason="in-app")` and returns True; on `A2AError` it posts one agent line in the same room `"could not record your answer: {message}"` via `create_agent_message` (event_id `a2a:{task_id}:answer-failed:{holder}`) and returns True (the press is still stored quietly).
  - Router web path: `handled = await a2a_tasks.answer_from_message(auth, resolve_owner_id(auth), request)` guarded by try/except that logs and sets `handled=False`; then `create_human_message(auth, room_id, request, wake=not handled)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/routers/test_a2a_card_answer.py (reuse a2a_env, client fixtures, patch messaging.create_human_message and create_agent_message with AsyncMock; create a held task via the internal ask-session route or send_message with a holding rules doc)
def test_notification_text_carries_the_openui_fence_and_index_row(...): assert 'openui-lang' in text and 'Button("Approve", "primary")' in text; get_task_by_notification returns (task_id, holder)
def test_holder_pressing_approve_decides_and_stores_quietly(...): post as holder with replyToMessageId=<notification id>, text "Approve" → task WORKING; create_human_message awaited with wake=False
def test_holder_pressing_deny_rejects(...): ...
def test_requester_pressing_approve_is_refused_and_agent_posts_the_reason(...): create_agent_message awaited with text starting "could not record your answer: self"; task still held
def test_non_holder_press_is_ignored_as_a_normal_message(...): wake=True, no decision
def test_reply_to_a_non_notification_is_a_normal_message(...): wake=True
def test_other_text_in_reply_to_notification_is_a_normal_message(...): "ok thanks" → wake=True
```

```ts
// openuiAsk.test.tsx addition
it("renders Approve/Deny and presses Approve", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  wrap('root = Stack([b])\nb = Buttons([Button("Approve", "primary"), Button("Deny", "secondary")])', { onReply });
  await userEvent.click(screen.getByRole("button", { name: "Approve" }));
  expect(onReply).toHaveBeenCalledWith("Approve", expect.any(String));
});
```

- [ ] **Step 2: Run to verify they fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** the new file, `tests/unit/routers/test_a2a_hold.py`, `tests/unit/routers/test_computer_person.py` (the native path must be untouched) and the vitest file — expected PASS. — [ ] **Step 5: Commit** — `git commit -m "feat(a2a): holders answer holds by pressing Approve/Deny in the DM"`

---

### Task 3: `when` clauses

**Files:**
- Modify: `apps/backend/core/services/a2a_rules.py` (`When`, `Rule.when`, `decide(..., operands: dict | None = None, intent: str | None = None, verified_by: str | None = None)`)
- Modify: `apps/backend/core/services/a2a_tasks.py` (`_decide_and_store`, `ask_session`, `_redecide` later: pass operands, intent, verified_by)
- Modify: `apps/backend/scripts/a2a_replay/cases.json` (+2 cases with `rules` overrides using `when`)
- Test: `apps/backend/tests/unit/services/test_a2a_rules.py` (add), `tests/unit/scripts/test_a2a_replay.py` (count ≥ 15)

**Interfaces:**
- Produces: `class When(ContractModel)`: `operands_equal: dict[str, str] | None = None`, `intent_matches: str | None = None`, `verified_by_in: list[str] | None = None`; `Rule.when: When | None = None`; `def when_matches(when: When | None, *, operands: dict | None, intent: str | None, verified_by: str | None) -> bool` (a `None` input for a set clause → False; `intent_matches` compares `" ".join(s.casefold().split())` forms).

- [ ] **Step 1: Write the failing tests**

```python
def test_when_intent_matches_allows_only_the_matching_intent(): ...        # rule allow with when.intent_matches "status of my own session"; "Status  of my OWN session" matches; "delete it" falls to default hold
def test_when_operands_equal_requires_every_key(): ...                     # {"text": "status?"} matches; operands lacking "text" → no match
def test_when_verified_by_in_restricts_proof(): ...                        # ["origin-message"] matches origin-message, not agent-runtime
def test_rule_with_when_never_matches_without_inputs(): ...                # decide called without operands/intent → no match
def test_deny_with_when_still_wins_over_exception(): ...
def test_replay_has_two_when_cases(): ...
```

- [ ] **Step 2: Run to verify they fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** `tests/unit/services/test_a2a_rules.py tests/unit/scripts/test_a2a_replay.py tests/unit/routers/test_a2a.py` — PASS. — [ ] **Step 5: Commit** — `git commit -m "feat(a2a): when clauses on rules (operands, intent, proof)"`

---

### Task 4: Re-deciding held tasks on a policy change

**Files:**
- Modify: `apps/backend/core/services/a2a_tasks.py` (`_decide_and_store` and `ask_session` store `requesterOrg`, `sessionPerson`, `originIsPerson`, `verifiedBy` on the row; new `_redecide`; `put_rules` stores first then re-decides; `decide`'s mismatch path at `:512` calls `_redecide` then continues if still held)
- Test: `apps/backend/tests/unit/routers/test_a2a_redecide.py`

**Interfaces:**
- Consumes: Task 3's `decide` inputs; `_dispatch`, `_leave`, `_record`, `_notify_holders`, `a2a_repo.list_held_tasks`.
- Produces: `async def _redecide(org: str, row: dict, rules: Rules, *, now: int) -> dict` returning the updated row; records `allow_by_policy`, `deny_by_policy`, `redecided`; `put_rules` order: store → re-decide each held task; rows lacking `requesterOrg` → `canceled` `policy_changed` (phase-1 behaviour).

- [ ] **Step 1: Write the failing tests**

```python
def test_rules_change_redecides_three_holds_into_allow_deny_and_hold(...):   # three held tasks; new doc allows one (dispatched, allow_by_policy), denies one (rejected, deny_by_policy), still holds one with a new holder set (redecided, notified again, policyRevision updated)
def test_hold_whose_holder_set_is_unchanged_is_not_renotified(...): ...
def test_redecide_to_allow_whose_dispatch_fails_is_failed_not_stuck(...): ...   # Review Focus 4
def test_decide_under_a_newer_revision_redecides_inline_and_proceeds(...): ...  # held task, doc changed but still holds → holder's allow succeeds; doc now denies → 409 policy_changed? no: task rejected deny_by_policy and the route returns 409 not_held
def test_pre_1b_row_without_inputs_is_canceled_policy_changed(...): ...
def test_expired_hold_still_cancels_as_expired(...): ...
```

- [ ] **Step 2: Run to verify they fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** the new file plus `tests/unit/routers/test_a2a_hold.py` — PASS. — [ ] **Step 5: Commit** — `git commit -m "feat(a2a): re-decide held tasks on a rules change"`

---

### Task 5: Signed Agent Card

**Files:**
- Modify: `apps/backend/pyproject.toml` (`uv add "a2a-sdk[signing]"`)
- Modify: `apps/backend/core/services/a2a_tasks.py` (`agent_card` → signed; new `_card_signer()` building the `PyJWK` from `network_proof._private()` and the protected header)
- Modify: `apps/backend/routers/a2a.py` (card route serializes `signatures`)
- Test: `apps/backend/tests/unit/routers/test_a2a.py` (card test asserts `signatures[0].protected` decodes to the header), `tests/integration/test_a2a_conformance.py` (verify with `create_signature_verifier` keyed by `kid` from `keys.json`; a tampered card fails)

**Interfaces:**
- Produces: `def signed_agent_card(org, agent_id, agent_name, base_url) -> AgentCard` (unsigned card when `NETWORK_PROOF_KEY` is unset, with a logged warning: Review Focus 5); protected header exactly `{"typ": "JOSE", "alg": "EdDSA", "kid": network_proof.kid(), "jku": f"{base_url}/a2a/keys.json"}`.

- [ ] **Step 1: Write the failing tests** (the two assertions above plus `test_card_without_issuer_key_is_unsigned_and_200`).
- [ ] **Step 2: Run to verify they fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** `tests/unit/routers/test_a2a.py tests/integration/test_a2a_conformance.py` — PASS. — [ ] **Step 5: Commit** — `git commit -m "feat(a2a): signed Agent Card with the issuer key"`

---

### Task 6: Extension page and spec follow the build

**Files:**
- Modify: `docs/a2a-authority-extension.md` (sections: OAuth clients and scopes; answering a hold from the DM; `when` clauses; re-deciding on a rules change; card signature; shrink "What phase 1 does not do")
- Test: `apps/backend/tests/unit/scripts/test_a2a_replay.py` already covers the cases; this task has no code test. Verification: `grep -n "clerk-oauth\|openui-lang\|verified_by_in\|allow_by_policy\|jku" docs/a2a-authority-extension.md` prints five hits.

- [ ] **Step 1: Write the page sections from the code as built** (every code, state and rule name grepped against the modules). — [ ] **Step 2: Run the grep** — five hits. — [ ] **Step 3: Commit** — `git commit -m "docs(a2a): extension page covers phase 1b"`

---

## Not in this plan
Phase 2 (network), 3 (spend), 4 (adapters); an in-app page for answering outside the DM; per-person vendor sign-in.
