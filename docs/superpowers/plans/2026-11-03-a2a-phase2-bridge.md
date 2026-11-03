# A2A phase 2, monorepo half: rules push and the cloud member bridge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The backend signs and pushes the workspace rules document to every laptop over its route link, hosted sends reach laptops through the workspace's cloud member, and a laptop session's reply comes back to the task.

**Architecture:** Three small changes on the phase-1b backend and the Hermes bridge. `put_rules` mints a `lightbulb-a2a-rules` JWS with the proof issuer key and pushes `rules.set` over every connected link of the workspace, and `network_route.serve` pushes it once on connect. The bridge gains one RPC method that declares the `a2a-<person>` session and calls `app.send` on the member socket with the `a2a` object. `_dispatch_session` tries that RPC first and falls back to the origin person's link.

**Tech Stack:** FastAPI backend (`apps/backend`), the Hermes bridge (`apps/infra/hermes/bridge`), pytest, `uv`.

**Spec:** `docs/superpowers/specs/2026-11-02-a2a-authority-extension-design.md` (phase 1) and the CLI repo's `docs/superpowers/specs/2026-11-02-a2a-phase2-network-design.md` §7–§8 (approved 2 Nov). The CLI half is `docs/superpowers/plans/2026-11-03-a2a-phase2-network.md` in the CLI repo.

## Global Constraints

- Branch `feat/a2a-phase2-bridge` stacked on `feat/a2a-phase1b` (it edits phase-1b code). Draft PR, never merge, never read production data, never print `NETWORK_PROOF_KEY` or any token. No new dependency.
- Rules JWS: compact EdDSA by `network_proof._sign`, `typ` `lightbulb-a2a-rules`, claims `{typ, workspace: <org id>, revision, doc, iat}`, no `exp`. `doc` is the stored rules document as `put_rules` stored it (camelCase, `exclude_none`).
- Link op name `rules.set` with body `{jws}`; an old engine answers `unsupported`, which is not an error (log at debug, continue).
- Bridge RPC `network.a2a_send` params `{to, text, request_id, a2a, person}`; it declares `{app: "a2a", person}` through the existing `cloud.sessions.set` path before `app.send`; auth is the existing bridge token.
- `_dispatch_session` order: bridge first when the workspace has a running cloud member (`hosted_mcp` knows the workspace's Hermes runtime; use the existing lookup), else `network_route.best_link` as in phase 1. Unchanged phase-1 contracts: the authority object, the rules document, `ask_session`'s tool body.
- Tests from `apps/backend`: `uv run pytest <files> -q --no-cov`; bridge tests from `apps/infra/hermes` the way its existing tests run (read its `pyproject`/`Makefile` first).

## Review Focus

1. A workspace with no stored rules document: on link connect the backend pushes the shipped default as a signed document, so laptops and the backend agree on `revision: "default"` (Task A).
2. A link whose engine answers `unsupported` to `rules.set` (old build): the connect handler continues and the link stays usable (Task A).
3. A hosted send to a workspace whose cloud member is down mid-request: the bridge RPC raises, `_dispatch_session` falls back to `best_link` within the same call (Task C).
4. The reply from a laptop session arrives for a task already `completed` as delivered: the completion is idempotent (one artifact, no second record) (Task C).
5. `rules.set` pushed to a link of a person who belongs to two workspaces: the push carries that link's workspace only (Task A).

---

### Task A: Mint and push the rules document

**Files:**
- Modify: `apps/backend/core/services/a2a_authority.py` (`sign_rules(org, doc, *, now) -> str`, `RULES_TYP = "lightbulb-a2a-rules"`)
- Modify: `apps/backend/core/services/a2a_tasks.py` `put_rules` (after the store and the re-decide: `await network_route.push_rules(org, jws)`), `apps/backend/core/services/network_route.py` (`push_rules(org, jws)`: `call(row, "rules.set", {"jws": jws}, 5.0)` on every connected link whose person belongs to `org`; `serve` pushes the current document once after the link is up)
- Test: `apps/backend/tests/unit/services/test_a2a_rules_push.py`

**Interfaces:**
- Consumes: `network_proof._sign(claims, typ=...)`, `a2a_repo.get_rules(org)`, `network_route.call(row, op, params, timeout_s)` (`network_route.py:158`), the links table of connected computers (`_connections()`, `:71`).
- Produces: `a2a_authority.sign_rules`, `network_route.push_rules(org, jws) -> int` (links reached).

- [ ] **Step 1: Write the failing tests:** `test_sign_rules_has_typ_workspace_revision_and_doc`, `test_put_rules_pushes_to_every_link_of_the_workspace` (two links of the org, one of another org: two calls), `test_push_continues_past_an_unsupported_engine` (Review Focus 2), `test_connect_pushes_the_default_document_when_none_is_stored` (Review Focus 1), `test_push_is_scoped_to_the_links_workspace` (Review Focus 5).
- [ ] **Step 2: Run** → FAIL. — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** the file plus `tests/unit/routers/test_a2a_hold.py tests/unit/routers/test_a2a_redecide.py` → PASS.
- [ ] **Step 5: Commit** `feat(a2a): the backend signs and pushes the rules document to every laptop`

---

### Task B: The bridge carries a hosted send through the member socket

**Files:**
- Modify: `apps/infra/hermes/bridge/server.py:495-540` (`RPC_METHODS["network.a2a_send"] = _network_a2a_send`: validate params, `network.declare_a2a_session(person)`, then `network.ask("app.send", {"app": "a2a", "person": person, "to": to, "text": text, "request_id": request_id, "a2a": a2a, "wait": True})`, return the receipt)
- Modify: `apps/infra/hermes/bridge/network.py:300-320` (`declare_a2a_session(person)`: add `{app: "a2a", person}` to the declared sessions if absent and call `cloud.sessions.set` with the same `members`/`revoked_keys` handling `reload_sessions` uses)
- Test: the bridge's test suite (new file beside the existing `network`/`server` tests; mocked socket)

**Interfaces:**
- Consumes: `network.ask`, `network.call`, `reload_sessions`'s params shape (`network.py:300-318`).
- Produces: RPC `network.a2a_send {to, text, request_id, a2a, person} -> receipt`.

- [ ] **Step 1: Write the failing tests:** `test_a2a_send_declares_then_sends_with_the_a2a_object` (socket mock records `cloud.sessions.set` then `app.send` with `a2a` intact), `test_a2a_send_with_missing_params_is_invalid_request`, `test_a2a_send_when_the_sidecar_is_off_is_unavailable`.
- [ ] **Step 2: Run** → FAIL. — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** the bridge suite → PASS.
- [ ] **Step 5: Commit** `feat(bridge): network.a2a_send carries a hosted send through the member socket`

---

### Task C: The backend prefers the bridge, and replies complete the task

**Files:**
- Modify: `apps/backend/core/services/a2a_tasks.py:412-450` (`_dispatch_session`: build the `a2a` object `{role: "ROLE_AGENT", parts: [{text: header + text}], metadata: {URI: row["authority"]}, extensions: [URI]}`; try the bridge RPC through the existing Hermes invocation client; on any exception or no runtime, fall back to the phase-1 `best_link` path), plus `on_session_reply(org, task_id, text)` (a reply to the `a2a-<person>` session completes the task with the reply text as the artifact when the task is `working` or `completed` as delivered; idempotent)
- Modify: `apps/infra/hermes/bridge/server.py` (the bridge forwards an inbox message addressed to an `a2a-<person>` session to the backend's internal route `POST /api/v1/internal/a2a/{ownerId}/session-reply` with the hook token, body `{request_id, text}`)
- Modify: `apps/backend/routers/a2a.py` (the internal route; the task is found by `dispatchRequestId == request_id`)
- Test: `apps/backend/tests/unit/routers/test_a2a_dispatch_bridge.py`

**Interfaces:**
- Consumes: Task B's RPC; the hook-token pattern of the existing internal ask-session route (`routers/a2a.py`); `a2a_repo.complete_task(org, task_id, *, artifact, record)` (`a2a_repo.py:189`).
- Produces: the internal session-reply route; `_dispatch_session` with the bridge-first order.

- [ ] **Step 1: Write the failing tests:** `test_dispatch_prefers_the_bridge_when_the_workspace_has_a_runtime` (RPC mock called with the a2a object; `best_link` not called), `test_dispatch_falls_back_to_best_link_when_the_bridge_raises` (Review Focus 3), `test_session_reply_completes_the_task_with_the_reply_text`, `test_session_reply_is_idempotent` (Review Focus 4), `test_session_reply_with_a_bad_hook_token_is_403`.
- [ ] **Step 2: Run** → FAIL. — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** the file plus `uv run pytest tests/unit -q -k a2a --no-cov` → PASS.
- [ ] **Step 5: Commit** `feat(a2a): hosted sends ride the cloud member; a session's reply completes the task`

---

## Not in this plan
The CLI half. Refusing card-less receivers (a founder call per the CLI spec's Clarifications). Phases 3 and 4.
