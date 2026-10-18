# Text agents, plan 4: migrate the line to Photon 2.0 and close the plan-2 review

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The workspace's texting number is a Photon dedicated iMessage line instead of a Sendblue line, bought and released by the Stripe webhook with the money-path fixes from plan 2's final review, with nothing else in the rail changing.

**Architecture:** Photon 2.0's REST API (`api.photon.codes/v1`) replaces the Sendblue client one for one: send, typing, read, dedicated-line request/release. Inbound is Photon's Standard-Webhooks delivery into a router shaped exactly like the Sendblue one; the reply addressing moves from phone numbers to Photon's `spaceId` + `senderId`, carried on the stored inbound row's `headers`. Because line creation is an asynchronous operation, provisioning becomes claim → take a pooled line (instant) or start an operation and mark the workspace pending; a one-minute sweeper completes operations and keeps the pool at `PHOTON_LINE_POOL_SIZE`. The plan-2 review's Stripe fixes land in the same tasks that touch those functions.

**Tech Stack:** FastAPI, httpx, DynamoDB (the address table and the billing row), Stripe, Next.js, CDK (secrets only).

**Spec:** `docs/superpowers/specs/2026-10-16-text-agents-design.md` (Facts: the Photon paragraph; Mechanisms: Inbound, Outbound, Numbers; Calls: add-on at $250). Vendor facts: `docs/research/2026-10-17-photon-integration-facts.md` (every UNCONFIRMED item there is an open question to Photon, listed in its §8).

**Builds on:** plan 1 (the rail: inbox rows with `channel`, `routers/agent_text.py`, `lightbulb-text.sh`, Mail panel, wipes), plan 2 (the add-on billing, the door, `text_lines.py`).

## Ponytail cuts

- No Node service and no Hermes Photon plugin in the runtime: the plugin is a personal gateway holding a gRPC stream; our runtimes are invoke-only, so inbound stays backend webhook → invoke, and Photon 2.0's REST send replaces the SDK.
- Media: the delivered text names the attachment (`[Attachment: <name>, <mimeType>]`); fetching bytes (`GET /attachments/{id}/content`) waits for a customer who sends one.
- No webhook-destination registration code: one `curl` per environment in the acceptance step.
- Pool default 0: the sweeper's refill is a config knob the owner flips once Photon's wait times are known.

## Global Constraints

- Every Photon call: base `https://api.photon.codes/v1`, `Authorization: Bearer {PHOTON_API_KEY}` (a `pho_sk_` project key), project `PHOTON_PROJECT_ID` (`pho_prj_…`); an `Idempotency-Key` header on every POST the spec marks as requiring one (messages, spaces, users, dedicated, read, destinations); a line costs $250 a month the moment it is claimed.
- Inbound verification is Standard Webhooks: headers `webhook-id`, `webhook-timestamp` (epoch seconds), `webhook-signature` (space-separated `v1,<base64>` entries); signed content `f"{id}.{timestamp}.{raw_body}"`; key = base64-decode of `PHOTON_WEBHOOK_SECRET` after its `whsec_` prefix; reject a timestamp more than 300 s from now; `hmac.compare_digest`; verify the RAW bytes, never a re-serialised body.
- The inbound body model is written from the event schema Photon publishes (`GET /v1/projects/{id}/webhooks/event-types/{eventType}/schema`), captured in Task 0 and committed as a fixture; the parser is not guessed.
- Empty `PHOTON_*` settings disable every door fail-closed (webhook `{"status":"disabled"}`, send `disabled`, provisioning raises).
- Money: a line is bought on `status == "active"` only; single-flight per owner with a leased claim; a bought line that cannot be recorded is released before the error propagates; release failures raise (Stripe redelivers) except a 404; the line is released on `customer.subscription.deleted` and when the item leaves the subscription, decided on a fresh `Subscription.retrieve`.
- `FRONT_DESK_PRICE_USD = 250` (display); `/billing/prices` and `/billing/account` return `front_desk_price_usd: null` while `STRIPE_FRONT_DESK_PRICE_ID` is empty, and the button hides on null.
- Everything that is not provider-specific (inbox rows, Reply-Key, `lightbulb-text.sh`, Mail panel, wipes' step order, the SOUL work) stays as plan 1 and 2 built it.

## Review Focus

1. A replayed delivery (same `webhook-id`) or a redelivered message (same message id, new `webhook-id`) must store and wake once. Test in Task 3.
2. A delivery signed with the legacy `X-Spectrum-Signature` only, or with a stale timestamp, must be 401, and a body that is not the captured schema must not 500 the route. Test in Task 3.
3. A reply must go to the customer's DM space as the line's own user, never as a caller-supplied id; a row with no `photon_space_id` is `bad_reply_key`. Test in Task 4.
4. Two `.updated` deliveries within a second, an empty pool, and a Photon operation that fails must end with at most one line requested and the claim released on failure. Tests in Task 5.
5. A trial workspace's first click must reach Checkout (not 400), and a canceled subscription must release the line. Tests in Task 6.

---

### Task 0: Capture the contract (ops, before Task 3)

Owner-owed first: a Photon account with the Business plan on one project, a `pho_sk_` project key, and the three dev secrets `lightbulb/dev/photon_api_key`, `lightbulb/dev/photon_project_id`, `lightbulb/dev/photon_webhook_secret` (the last comes from Step 3).

- [ ] **Step 1:** `curl -s -H "Authorization: Bearer $KEY" https://api.photon.codes/v1/projects/$PRJ/webhooks/event-types` → pick the inbound message event (expected name like `message.received`; record the exact string).
- [ ] **Step 2:** `curl -s -H "Authorization: Bearer $KEY" https://api.photon.codes/v1/projects/$PRJ/webhooks/event-types/<event>/schema > apps/backend/tests/fixtures/photon/<event>.schema.json`; commit it. Read it and write down, in the Task 3 brief's "Pinned fields" block: the message id path, the sender's user id and phone path, the space id path, the line's phone (or line/user id) path, the text path, the attachment name/mimeType paths, the direction field, the timestamp field, the service/platform field.
- [ ] **Step 3:** Register the dev destination once: `curl -s -X POST -H "Authorization: Bearer $KEY" -H "Idempotency-Key: $(uuidgen)" -H 'Content-Type: application/json' https://api.photon.codes/v1/projects/$PRJ/webhooks/destinations -d '{"apiVersion":"<from the spec>","enabledEvents":["<event>"],"name":"lightbulb-dev","url":"https://api-dev.thelightbulb.company/api/v1/webhooks/photon-inbound","status":"enabled"}'` → save the one-time `whsec_…` as the webhook secret.
- [ ] **Step 4:** Buy one dev line: `POST /v1/projects/$PRJ/platforms/imessage/dedicated` with an `Idempotency-Key`; poll `GET …/platforms/operations/{operationId}` until done; `GET …/platforms/resources` to read the line's `resourceId`, phone number and its agent user id (`pho_usr_…`). Record all three for Task 10.

### Task 1: Photon secrets and settings; Sendblue secrets removed

**Files:** `apps/backend/core/config.py` (replace the three `SENDBLUE_*` with `PHOTON_API_KEY`, `PHOTON_PROJECT_ID`, `PHOTON_WEBHOOK_SECRET`, all `str = ""`; add `PHOTON_LINE_POOL_SIZE: int = 0`; `FRONT_DESK_PRICE_USD = 250`), `apps/infra/lib/lightbulb-stage.ts`, `apps/infra/lib/stacks/service-stack.ts` (`SecretNames` + secrets map), the six fixtures that enumerate `secretNames`, `apps/infra/test/service-stack.test.ts` (the Sendblue test becomes the Photon test).
- [ ] Failing test: the task def carries `PHOTON_API_KEY`, `PHOTON_PROJECT_ID`, `PHOTON_WEBHOOK_SECRET` and no `SENDBLUE_*`.
- [ ] Implement; `npx vitest run` the touched infra tests; `CDK_DOCKER=/usr/bin/false npx cdk synth`; `deploy.yml` unchanged.
- [ ] Commit: `text agents: Photon secrets replace Sendblue's`.

### Task 2: The Photon client

**Files:** Create `apps/backend/core/services/photon.py`; delete `core/services/sendblue.py` and `tests/unit/services/test_sendblue.py`; test `tests/unit/services/test_photon.py` (httpx `MockTransport` through a module-level `_transport` seam, as the Sendblue tests did).
**Produces** (all on `_call(method, path, json=None, *, idempotency_key=None) -> dict`, raising `PhotonError(status, code)` on non-2xx with `code` from the error body's code/type field else `http_<status>`; `code == "not_found"` for 404):
- `enabled() -> bool` (all three settings non-empty)
- `ensure_user(phone: str) -> str` → `POST /projects/{prj}/users` (body per spec; `Idempotency-Key` = `f"user:{phone}"`); returns the `pho_usr_` id (on a conflict/existing response, the existing id; the implementer reads the spec's response shape).
- `ensure_dm_space(*, line_user_id: str, customer_user_id: str) -> str` → `POST /projects/{prj}/spaces` `{"platform": "imessage", "participantIds": [line_user_id, customer_user_id]}` with `Idempotency-Key` = `f"space:{line_user_id}:{customer_user_id}"`; returns the space id.
- `send_text(*, sender_id: str, space_id: str, text: str, idempotency_key: str) -> dict` → `POST /projects/{prj}/messages` `{"senderId", "spaceId", "content": {"type": "text", "format": "plain", "text"}}`; returns the Message (202). **Dry run:** when `space_id` starts with `dry:` it does not call Photon and returns `{"id": f"dry-{uuid4().hex}", "status": "accepted", "dry_run": True}` (the bench).
- `send_typing(*, sender_id, space_id, typing: bool = True) -> None` → `POST /projects/{prj}/spaces/{space_id}/typing`; swallows errors.
- `mark_read(message_id: str) -> None` → `POST /projects/{prj}/messages/{message_id}/read` with an `Idempotency-Key`; swallows errors.
- `request_dedicated_line(*, idempotency_key: str) -> dict` → `POST /projects/{prj}/platforms/imessage/dedicated` → the operation (`operationId`, status).
- `get_operation(operation_id: str) -> dict`; `cancel_operation(operation_id: str) -> None` (best effort).
- `get_resource(resource_id: str) -> dict` → `GET /projects/{prj}/platforms/resources/{resource_id}` (phone number + the line's agent user id; field names from the spec).
- `release_line(resource_id: str) -> None` → `DELETE /projects/{prj}/platforms/imessage/dedicated/{resource_id}`; 404 → return (already gone).
- [ ] Tests: headers (Bearer, Idempotency-Key present where required), exact bodies, the error mapping, the `dry:` short-circuit, typing/read swallow, release 404 → None.
- [ ] Commit: `text agents: Photon client replaces the Sendblue client`.

### Task 3: The inbound webhook

**Files:** Create `apps/backend/routers/photon_inbound.py` (move the provider-neutral helpers, `TEXT_WAKES_*`, `text_session_key`, `build_delivered_text`, `paused_autoreply`, `_business_name`, the handler order, out of `sendblue_inbound.py` into it, then delete `sendblue_inbound.py` and its test); `apps/backend/main.py` (mount `/api/v1/webhooks/photon-inbound`); test `tests/unit/routers/test_photon_inbound.py` (the Sendblue tests, re-shaped; a `_sign(body)` helper producing the Standard-Webhooks headers from a test secret).
**Pinned fields:** filled from Task 0's fixture (message id, sender user id + phone, space id, line phone/user id, text, attachments, direction, timestamp, service).
**Handler order** (unchanged from plan 1 except where named): `disabled` → signature (401 `bad_signature`; 401 `stale_timestamp` past 300 s) → not the inbound-message event or direction not inbound → `ignored_event` → dedup on `photon:{message_id}` → line = `agent_email_repo.get_by_address(<line phone>)` with `kind == "text"` (the sweeper writes the line phone on the row, so the phone path is the lookup; no new index) → `customer` = sender phone, else `f"photon:{sender_user_id}"`; empty → `dropped_no_sender` → `team_address` → store with `put_message(..., headers={"photon_space_id": <space id>, "photon_sender_user_id": <sender user id>, "photon_message_id": <id>}, channel="text")` → human thread → capped autoreply (gated by `text_paused`, sent with `photon.send_text(sender_id=line["photon_user_id"], space_id=<space id>, ...)`) → stranger caps → `send_typing` + `mark_read` best effort → `deliver_to_agent` (same message template; `Via:` from the service field). Metric `text.inbound`.
- [ ] Tests: the plan-1 list re-shaped plus `test_replayed_webhook_id_is_duplicate`, `test_redelivered_message_id_is_duplicate`, `test_legacy_signature_only_is_401`, `test_stale_timestamp_is_401`, `test_unknown_body_shape_is_422_not_500` (an unexpected but signed body → 422 from the model, no 500).
- [ ] Commit: `text agents: Photon inbound webhook replaces Sendblue's`.

### Task 4: Replies through Photon

**Files:** `apps/backend/routers/agent_text.py` (`send_reply`), `apps/infra/hermes/skills-src/lightbulb/references/text.md` (no provider name), `tests/unit/routers/test_agent_text.py`.
**Change:** `send_reply` sends with `photon.send_text(sender_id=line["photon_user_id"], space_id=row["headers"]["photon_space_id"], text=text, idempotency_key=f"text-reply:{row['received_key']}:{sha256(text)[:16]}")`; a row without `photon_space_id` is `bad_reply_key`; `(PhotonError, httpx.HTTPError)` → `send_failed`; `put_sent(message_id=resp.get("id") or f"photon-reply:{uuid4().hex}", ...)`. Everything else (disclosure, human fence, caps) unchanged.
- [ ] Tests: exact `send_text` kwargs, the missing-space refusal, the idempotency key shape.
- [ ] Commit: `text agents: replies go through Photon`.

### Task 5: Provisioning: claim, pool, operation, sweeper

**Files:** `apps/backend/core/services/text_lines.py`, `core/repositories/agent_email_repo.py` (`put_text_line(..., photon_resource_id, photon_user_id)`, `assign_pooled_line(number, owner_id, agent_id) -> bool` (conditional on `owner_id = "__pool__"`), `put_pending_line(operation_id, owner_id, idempotency_key)` as a row `{address: f"pending:{operation_id}", owner_id, kind: "text_pending", created_at}`, `list_pending_lines()` (one filtered scan; `# ponytail: a scan of a small table once a minute; a GSI when it is not small`), `delete_pending_line(operation_id)`), `core/repositories/billing_repo.py` (`claim_front_desk` becomes a lease: `SET front_desk = true, front_desk_claim_at = :now` with condition `attribute_not_exists(front_desk) OR front_desk = false OR front_desk_claim_at < :stale` (`:stale` = now − 600 s); `set_front_desk(False)` also `REMOVE front_desk_claim_at`), `main.py` (register the sweeper in `system_health.BACKGROUND_TASKS` beside the other one-minute sweepers), tests `tests/unit/services/test_text_lines.py`, repo tests.
**Produces:**
- `ensure_line(owner_id) -> "unchanged" | "provisioned" | "pending" | "no_agent"`: row → `unchanged`; `owner_row` missing or not active → raise (transient); no `customer_operations` → metric `text_line.no_front_desk_agent`, return `no_agent` (permanent, no raise); claim (lease) → False → `unchanged`; a pooled line (`query_by_owner("__pool__")` first row) → `assign_pooled_line` → soul refresh → `provisioned`; else `request_dedicated_line(idempotency_key=f"front-desk:{owner_id}:{claim_at}")` → `put_pending_line` → `pending`. Failure after the request → `cancel_operation` best effort, release the claim, metric, raise.
- `release_line(owner_id) -> "absent" | "released"`: a pending row → `cancel_operation` + delete it; a line row → `photon.release_line(resource_id)` (raise on failure except 404), `delete_text_line`, `set_front_desk(False)`, soul refresh; no row → clear the flag → `absent`.
- `sync_front_desk(owner_id, *, subscription_id)`: `stripe.Subscription.retrieve(subscription_id)` → decide on its CURRENT `status` and items (`active` + item → `ensure_line`; item gone or `canceled` → `release_line`; else `skipped`).
- `sweep_text_lines()` (every 60 s): for each pending row → `get_operation`: succeeded → `get_resource(resourceId)` → `put_text_line(number, owner_id, agent_id, photon_resource_id, photon_user_id)` (or, for `__pool__`, the pool row) → delete the pending row → soul refresh + `mail_changed` → metric; failed/cancelled → delete the pending row, release the owner's claim, metric `text_line.provision_failed`; then pool refill: `len(pool rows) + len(pending pool rows) < settings.PHOTON_LINE_POOL_SIZE` → request lines for `__pool__`.
- [ ] Tests: the plan-2 list re-shaped plus `test_pooled_line_is_assigned_instantly`, `test_empty_pool_starts_an_operation_and_returns_pending`, `test_sweeper_completes_a_succeeded_operation`, `test_sweeper_failed_operation_releases_the_claim`, `test_sweeper_refills_the_pool_to_size`, `test_lease_is_reclaimable_after_ten_minutes`, `test_sync_decides_on_the_fresh_subscription`.
- [ ] Commit: `front desk: Photon lines via claim, pool and sweeper`.

### Task 6: The plan-2 review's Stripe fixes

**Files:** `apps/backend/routers/billing.py` (the door), `core/services/billing_service.py`, `core/services/stripe_webhook_handlers.py`, `schemas/billing.py`, tests `tests/unit/routers/test_billing_front_desk.py`, `tests/unit/services/test_trial_checkout.py`, `test_seat_billing.py`, `test_stripe_webhook_handlers.py`.
- Door: the plan name passed to `create_workspace_checkout` is the one it accepts (`"workspace"` per `plan_retired` at billing_service.py:183; verify, do not assume); a precheck `starter_agent_ids["customer_operations"]` → 409 `no_front_desk_agent`; one router test that reaches the real `create_workspace_checkout` plan check.
- `handle_subscription_deleted`: `release_line(owner_id)` when `event_data["id"] == account.get("stripe_subscription_id")` (read before `cancel_subscription`); handler test.
- `handle_subscription_state`: `sync_front_desk(owner_id, subscription_id=event_data["id"])` after the seat block, for every status.
- Regression from plan 2 Task 5: `tests/unit/routers/test_billing.py`'s `TestStripeWebhook` subscription tests fail (`sync_front_desk` → `query_by_owner` on a table that fixture never creates); give that class the same autouse `sync_front_desk` mock `test_stripe_webhook_handlers.py` uses, and run that file in this task.
- Pending-session reuse: metadata `front_desk: "1"/"0"` on the Checkout session and in the reuse predicate (a plain-Team session is never handed to a front-desk click or the reverse).
- `add_front_desk_item`: only `active` or `trialing`; on `trialing`, `stripe.Subscription.modify(sub_id, items=[{price, quantity: 1}], trial_end="now", proration_behavior="always_invoice", idempotency_key=...)` (the paid plan starts now: spec Call "no trial carried over"); on `active`, `SubscriptionItem.create(..., proration_behavior="always_invoice")`.
- `/billing/prices` and `/billing/account`: `front_desk_price_usd: int | None`, `None` when `settings.STRIPE_FRONT_DESK_PRICE_ID` is empty.
- [ ] Tests for each bullet; commit: `billing: front desk review fixes (door plan, deleted handler, fresh retrieve, session metadata, trial end, invoice now, null price)`.

### Task 7: The app: pending state and the hidden button

**Files:** `apps/frontend/src/components/mail/MailPanel.tsx`, `apps/frontend/src/lib/pricing.ts` / `hooks/useBilling.ts` types, tests.
- Button hidden when `front_desk_price_usd` is null; "Setting up your number…" (no button) while `account.front_desk` is true and there is no line; error words ("Could not start checkout", "Not available on this plan") instead of HTTP codes.
- [ ] Tests; commit: `app: number pending state; button hidden without a price`.

### Task 8: Wipes and skill wording

**Files:** `core/services/account_wipe.py` (`release_text_lines` → `photon.release_line(row["photon_resource_id"])` best effort, pending rows cancelled), `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md` (anchor names `routers/photon_inbound.py`), plans 1-3 (a one-line note at the top: "provider migrated to Photon by plan 4").
- [ ] Tests (the two wipe failure tests re-pointed); commit: `wipes release Photon lines; docs point at Photon`.

### Task 9: Plan 3's bench speaks Photon

A note for plan 3 Task 6: the bench posts Standard-Webhooks-signed bodies in the captured schema for `+1555` customers, and the dry run is `space_id` starting `dry:`; the inbox check is unchanged.

### Task 10: Dev acceptance

- [ ] Stripe sandbox: the "Front desk" Price is $250; `STRIPE_FRONT_DESK_PRICE_ID_DEV` set.
- [ ] With `PHOTON_LINE_POOL_SIZE=0`: on a trial workspace click "Get a phone number", pay with a test card, confirm `pending` → the sweeper completes the operation → the number shows; text it from a phone; agent reply in blue; disclosure once; Take over silences; capped autoreply once per day. Record the operation's wall time.
- [ ] Set `PHOTON_LINE_POOL_SIZE=1` in dev; confirm the sweeper buys one pooled line and the next workspace's click assigns it instantly.
- [ ] Remove the add-on item in the Stripe sandbox dashboard → the line is released and the row gone; cancel the subscription on another workspace → released through `handle_subscription_deleted`. Receipts on the PR.

## Self-review notes

- Spec coverage: Facts (Photon paragraph), Mechanisms Inbound/Outbound/Numbers, Calls (add-on at $250); the plan-2 final review's two Criticals and Importants 1-8 each map to a bullet in Tasks 5-7; its Minors not folded: `/billing/account.front_desk` reflects the claim (acceptable with the pending state), `_RowExists` owner logging.
- Open until Photon answers (fact sheet §8): line wait times (pool size), per-line limits, refund on release, the SMS fallback sender, delivery-status events. None blocks the build; the pool knob and the metric on `provision_failed` cover the first two.
