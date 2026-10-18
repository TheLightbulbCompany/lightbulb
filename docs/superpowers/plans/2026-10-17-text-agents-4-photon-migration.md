# Text agents, plan 4: the line on Photon 2.0, and the plan-2 review closed

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Rewritten 2026-10-17 after the owner chose Photon 2.0 (over Sendblue, and over Photon's stable API), after an adversarial review and a spec-level verification (`docs/research/2026-10-17-photon-assumptions-verified.md`, `docs/research/2026-10-17-hermes-photon-plugin-fit.md`), and after the owner added member routing ("one number; a member's text is routed like a room message, a customer's goes to the front desk").

**Goal:** The workspace's texting number is a Photon 2.0 dedicated iMessage line, bought and released by the Stripe webhook; customers reach the front desk, members reach the team; nothing else in the rail changes.

**Architecture:** Photon 2.0's REST API (`api.photon.codes/v1`) replaces the Sendblue client one for one: send, typing, read, dedicated-line request and release. Inbound is Photon's `message.received` event, verified as a Standard Webhook, into a router shaped exactly like the Sendblue one; the reply addressing moves to Photon's `spaceId` plus the line's own user id, carried on the stored inbound row. Line creation is an asynchronous operation: the webhook starts it, waits a few seconds for the fast case, and otherwise records the workspace as pending; the Mail panel's line read completes the operation when Photon has finished (the repo's "advance on read" pattern), so there is no pool of idle lines and no sweeper. Member routing posts a member's text into the main room as that member, so listen modes and Jev pick the agent unchanged.

**Tech Stack:** FastAPI, httpx, DynamoDB (the address table and the billing row), Stripe, Next.js, CDK (secrets only).

**Spec:** `docs/superpowers/specs/2026-10-16-text-agents-design.md` (Rules: the two kinds of texter; Facts: the Photon paragraph; Mechanisms: Inbound, Outbound, Numbers; Calls: add-on at $250). Vendor facts: `docs/research/2026-10-17-photon-integration-facts.md` §8 lists the questions still open with Photon.

**Builds on:** plan 1 (the rail), plan 2 (the add-on billing, the door, `text_lines.py`), plan 3 (SOUL, reference, OpenCLI).

## Already done on this branch

- Task 1 (ae8e22aa7): Photon secrets replace Sendblue's in CDK; `PHOTON_*` settings; `FRONT_DESK_PRICE_USD = 250`. The `SENDBLUE_*` settings and the Sendblue client/webhook stay until Task 12.
- Task 6 (2de3c9d6b, 22fe0fc75): the plan-2 review's Stripe fixes (door plan name + Customer Operations precheck, release on `customer.subscription.deleted`, fresh `Subscription.retrieve`, pending-session metadata, trial end on add, `always_invoice`, null price, stale-subscription guard).
- Task 7 (1525e9ba3): the Mail panel's pending state, the button hidden without a price, plain error words.
- The inbound event schema fixture: `apps/backend/tests/fixtures/photon/message.received.imessage.schema.json`, extracted from `https://api.photon.codes/openapi.json` (`ImessageMessageReceivedEvent`, apiVersion `2026-07-01`).

## Ponytail cuts

- No pool, no sweeper, no leader election: the webhook's short poll takes the fast case; the Mail panel's line read completes the slow one; a Stripe event also completes it. `PHOTON_LINE_POOL_SIZE` is removed in Task 8.
- No Node service and no Hermes Photon plugin: its receive half is a gRPC stream in a Node sidecar (verified in its source), which an invoke-only runtime cannot hold.
- Media: the delivered text names the attachment (`[Attachment: <attachmentId>]`); fetching bytes waits for a customer who sends one.
- No webhook-destination registration code: one `curl` per environment (Task 0).
- The Sendblue client and webhook are deleted only after Photon passes dev acceptance (Task 12), so the branch stays reversible until then.

## Global Constraints

- Every Photon call: base `https://api.photon.codes/v1`, `Authorization: Bearer {PHOTON_API_KEY}` (`pho_sk_…`), project `PHOTON_PROJECT_ID` (`pho_prj_…`), an `Idempotency-Key` header on every POST the spec requires one for (messages, spaces, users, dedicated, read, destinations). A line costs $250 a month from the moment Photon claims it.
- Inbound verification is Standard Webhooks (Photon issues a `whsec_` secret): headers `webhook-id`, `webhook-timestamp` (epoch seconds), `webhook-signature` (space-separated `v1,<base64>` entries); signed content `f"{id}.{timestamp}.{raw_body}"`; key = base64-decode of the secret after `whsec_`; reject a timestamp more than 300 s from now; `hmac.compare_digest`; verify the RAW bytes. The header names are pinned from the first captured dev delivery (Task 0); if Photon sends `X-Spectrum-*` instead, the verifier's header names change and nothing else.
- The inbound body is parsed by the committed fixture's shape (pinned fields below), never guessed. Pinned fields of a `message.received` event: envelope `id` (`pho_evt_…`), `type == "message.received"`, `apiVersion`, `source == "imessage.photon"`, `timestamp`, `data.projectId`, `data.object`; the object: `id` (message id), `platform == "imessage"`, `status == "received"`, `space.id`, `space.participantIds[]`, `sentBy.id` + `sentBy.handle` (the customer's user id and phone or Apple-ID email), `receivedBy.id` + `receivedBy.handle` (OUR line's user id and number; optional in the schema), `receivedBy.details.resourceId` (the dedicated-line resource), `content.type` (`text` with `content.text`; `attachment` with `content.attachmentId`; `read`, `typing` and `group` are not messages), `timestamps.receivedAt`. There is no `direction` and no `service` field.
- Empty `PHOTON_*` settings disable every door fail-closed (webhook `{"status":"disabled"}`, send `disabled`, provisioning raises).
- Money: a line is requested on `status == "active"` only; one request per claim; a request that cannot be recorded is cancelled or released before the error propagates; release failures raise (Stripe redelivers) except a 404; the line is released on `customer.subscription.deleted` and when the item leaves the subscription.
- Member routing: a texter whose number is in the line row's `member_phones` is a member; their text is posted into the workspace's default room as that member and is never delivered to the front desk; it still gets an inbox row and a Reply-Key so the agent can text back; no disclosure prefix and no stranger caps for members; everything else is a customer.
- Everything that is not provider-specific stays as plans 1 to 3 built it.

## Review Focus

1. A replayed delivery (same `webhook-id`) or a redelivered message (same message id) must store and wake once; a `read`/`typing`/`group` content event must be acknowledged and never stored. Test in Task 3.
2. A delivery with a bad or stale signature must be 401; a signed body that is not the fixture's shape must be 422, never 500. Test in Task 3.
3. A reply must go to the inbound row's space as the line's own user, never a caller-supplied id; a row without provider ids is `bad_reply_key`. Test in Task 4.
4. Two `.updated` deliveries within a second must request at most one line; a pending workspace whose operation failed must get its claim back; completion on read must record the line exactly once across concurrent reads. Tests in Task 5.
5. A member's text must land in the main room as that member and never wake the front desk; the same phone unregistered is a customer again. Tests in Task 11.

---

### Task 0: Project, secrets, first real calls (owner-owed, with the controller)

Owner: a Photon account with the Business plan on one project, a `pho_sk_` project key, the three dev secrets `lightbulb/dev/photon_api_key`, `lightbulb/dev/photon_project_id`, `lightbulb/dev/photon_webhook_secret`, and the same three in prod before any promotion (any value; empty disables the rail). The Stripe sandbox "Front desk" Price at $250 and `STRIPE_FRONT_DESK_PRICE_ID_DEV`.

- [ ] Register the dev destination once (`enabledEvents: ["message.received"]`, `url: https://api-dev.thelightbulb.company/api/v1/webhooks/photon-inbound`, the `apiVersion` the spec names) and save the one-time `whsec_…` secret.
- [ ] Buy one dev line through the API, poll the operation to `succeeded`, then find the line's own user: list the project's iMessage users and take the one whose `details.resourceId` equals the operation's `resourceId`; record its `handle` (the number) and `id` (`pho_usr_…`). Record the wall time of the operation; this number decides whether the short in-webhook poll ever matters.
- [ ] Text the dev line once and read the backend log: pin the real signature header names (Standard Webhooks or `X-Spectrum-*`) and confirm the body matches the fixture.

### Task 1: Photon secrets and settings — DONE (ae8e22aa7)

### Task 2: The Photon client

**Files:** Create `apps/backend/core/services/photon.py`; test `tests/unit/services/test_photon.py` (httpx `MockTransport` through a module-level `_transport` seam, as `test_sendblue.py` does). Leave `sendblue.py` in place.
**Produces** (all on `_call(method, path, json=None, *, idempotency_key=None) -> dict`, raising `PhotonError(status, code)` on non-2xx with `code` from the problem body's `code`/`type` field else `http_<status>`; a 404 raises `PhotonError(404, "not_found")`):
- `enabled() -> bool` (the three settings non-empty)
- `send_text(*, sender_id: str, space_id: str, text: str, idempotency_key: str) -> dict` → `POST /projects/{prj}/messages` `{"senderId", "spaceId", "content": {"type": "text", "format": "plain", "text"}}` → the Message (202). Dry run: a `space_id` starting `dry:` never calls Photon and returns `{"id": f"dry-{uuid4().hex}", "status": "accepted", "dry_run": True}` (the bench).
- `send_typing(*, sender_id, space_id, typing: bool = True) -> None` → `POST /projects/{prj}/spaces/{space_id}/typing`; swallows every error.
- `mark_read(message_id: str) -> None` → `POST /projects/{prj}/messages/{message_id}/read` with an Idempotency-Key; swallows every error.
- `request_dedicated_line(*, idempotency_key: str) -> dict` → `POST /projects/{prj}/platforms/imessage/dedicated` → the operation.
- `get_operation(operation_id: str) -> dict`; `cancel_operation(operation_id: str) -> None` (409 = already finished, swallowed; other errors raise).
- `find_line_user(resource_id: str) -> dict | None` → list the project's iMessage users (`GET /projects/{prj}/users`, filter by platform if the spec offers one, paging) and return the one with `details.resourceId == resource_id` (`{"id", "handle"}`).
- `release_line(resource_id: str) -> None` → `DELETE /projects/{prj}/platforms/imessage/dedicated/{resource_id}`; 200 or 202 → return; 404 → return.
- [ ] Tests: Bearer and Idempotency-Key headers where required, exact bodies, the problem-code mapping, the `dry:` short-circuit, typing/read swallow, `find_line_user` paging and match, release 404/202.
- [ ] Commit: `text agents: Photon client`.

### Task 3: The inbound webhook

**Files:** Create `apps/backend/routers/photon_inbound.py`; move the provider-neutral helpers out of `routers/sendblue_inbound.py` into a shared module `core/services/text_inbound.py` (`TEXT_WAKES_*`, `text_session_key`, `build_delivered_text`, `paused_autoreply`, `_business_name`) and import them from both routers (Sendblue stays importable until Task 12); `apps/backend/main.py` (mount `/api/v1/webhooks/photon-inbound`); `core/repositories/agent_mail_inbox_repo.py` (`put_message(..., provider_ids: dict | None = None)` stored as a `provider` map only when given; `put_message` keeps only three mail headers, so the ids cannot ride `headers`); tests `tests/unit/routers/test_photon_inbound.py` (a `_sign(body)` helper producing the Standard-Webhooks headers from a test secret; bodies built from the fixture's shape), `tests/unit/repositories/test_agent_mail_inbox_repo.py`.
**Handler order** (plan 1's, with the provider parts swapped): `disabled` → signature (401 `bad_signature`, 401 `stale_timestamp`) → the envelope's `type != "message.received"` or the object's `status != "received"` or `content.type` not in (`text`, `attachment`) → `ignored_event` → dedup on `photon:{object.id}` → line = `agent_email_repo.get_by_address(receivedBy.handle)` with `kind == "text"`, falling back to `agent_email_repo.text_line_by_photon_user(receivedBy.id)` (a filtered `query_by_owner` is impossible here; add `text_line_by_photon_user` as one `scan` with a filter on `photon_user_id`, `# ponytail: rare path (handle missing); a GSI if it is ever hot`) → none → `dropped_unknown_line` → `customer = sentBy.handle` (else `f"photon:{sentBy.id}"`; empty → `dropped_no_sender`) → **member check** (Task 11 inserts here) → `team_address` → store `put_message(..., from_addr=customer, text=<content.text or "[Attachment: <attachmentId>]">, channel="text", provider_ids={"space_id": space.id, "sender_user_id": sentBy.id, "line_user_id": receivedBy.id, "message_id": object.id})` → human thread → capped autoreply (gated by `text_paused`, sent with `photon.send_text(sender_id=line["photon_user_id"], space_id=space.id, ...)`) → stranger caps → `send_typing` + `mark_read` best effort → `deliver_to_agent` (same template; `Via: iMessage`). Metric `text.inbound`.
- [ ] Tests: plan 1's list re-shaped plus `test_replayed_webhook_id_is_duplicate`, `test_redelivered_message_id_is_duplicate`, `test_read_and_typing_events_are_ignored`, `test_stale_timestamp_is_401`, `test_unknown_body_shape_is_422_not_500`, `test_line_found_by_user_id_when_handle_missing`, `test_provider_ids_are_stored`.
- [ ] Commit: `text agents: Photon inbound webhook`.

### Task 4: Replies through Photon

**Files:** `apps/backend/routers/agent_text.py` (`send_reply`), `apps/infra/hermes/skills-src/lightbulb/references/text.md` (no provider name), `tests/unit/routers/test_agent_text.py`.
**Change:** `send_reply` sends with `photon.send_text(sender_id=line["photon_user_id"], space_id=row["provider"]["space_id"], text=text, idempotency_key=f"text-reply:{row['received_key']}:{sha256(text)[:16]}")`; a row without `provider.space_id` is `bad_reply_key`; `(PhotonError, httpx.HTTPError)` → `send_failed`; `put_sent(message_id=resp.get("id") or f"photon-reply:{uuid4().hex}", ...)`; no disclosure prefix when `customer in line.get("member_phones", {})` (Task 11 fills that map; the check is harmless before). Everything else unchanged.
- [ ] Tests: exact `send_text` kwargs, the missing-provider refusal, the key shape, no disclosure for a member phone.
- [ ] Commit: `text agents: replies go through Photon`.

### Task 5: Provisioning: claim, request, short poll, complete on read

**Files:** `apps/backend/core/services/text_lines.py`, `core/repositories/agent_email_repo.py` (`put_text_line(*, number, owner_id, agent_id, photon_resource_id, photon_user_id)`; `put_pending_line(*, operation_id, owner_id, agent_id)` as a row `{address: f"pending:{owner_id}", owner_id, agent_id, kind: "text_pending", operation_id, created_at}` (one pending row per owner, conditional on `attribute_not_exists(address)`); `pending_line_for_owner(owner_id)`; `delete_pending_line(owner_id)` conditional on `kind = :pending`), `core/repositories/billing_repo.py` (`claim_front_desk` stays a conditional claim: `attribute_not_exists(front_desk) OR front_desk = false`; no time lease — the pending row is the in-flight marker and `front_desk=True` with neither a line nor a pending row is repaired by `ensure_line` itself: see below), `routers/agent_text.py` (`GET /line` calls `text_lines.complete_pending(owner_id)` before answering), tests `tests/unit/services/test_text_lines.py`, repo tests, `tests/unit/routers/test_agent_text.py`.
**Produces:**
- `ensure_line(owner_id) -> "unchanged" | "provisioned" | "pending" | "no_agent"`: a line row → `unchanged`; a pending row → `complete_pending(owner_id)` and return its result (`provisioned` | `pending`); no Customer Operations → metric `text_line.no_front_desk_agent`, `no_agent`; `claim_front_desk` False with neither row → the flag is stale from a crash: `set_front_desk(False)` then claim again once; claimed → `op = request_dedicated_line(idempotency_key=f"front-desk:{owner_id}:{uuid4()}")` → `put_pending_line(operation_id=op["operationId"], ...)` (on a put failure: `cancel_operation` best effort, `set_front_desk(False)`, raise) → `complete_pending(owner_id, wait_s=10)`.
- `complete_pending(owner_id, *, wait_s: float = 0) -> "provisioned" | "pending" | "absent"`: read the pending row (`absent` when none); poll `get_operation` every 2 s until `wait_s` is spent or the state is terminal; `succeeded` → `user = find_line_user(op["resourceId"])` → `put_text_line(number=user["handle"], ..., photon_resource_id=op["resourceId"], photon_user_id=user["id"])` → `delete_pending_line` (conditional; a lost race means another reader recorded it: still `provisioned`) → `refresh_agent_soul` + `mail_changed` best effort → metric → `provisioned`; `failed`/`cancelled` → `delete_pending_line`, `set_front_desk(False)`, metric `text_line.provision_failed{code}` → raise `BillingServiceError("line_provision_failed")` from the webhook path (Stripe retries) but return `"absent"` from the read path (the panel shows the button again); still running → `pending`. If `put_text_line` returns None or raises after a succeeded operation: `release_line(resourceId)` best effort (warning naming the number), `set_front_desk(False)`, metric, raise.
- `release_line(owner_id) -> "absent" | "released"`: a pending row → `cancel_operation` (409 → the line exists: fall through to a normal release after `complete_pending`), delete it, clear the flag → `released`; a line row → `photon.release_line(resource_id)` (raise on failure except 404), `delete_text_line`, `set_front_desk(False)`, soul refresh → `released`; nothing → clear the flag → `absent`.
- `sync_front_desk(owner_id, *, subscription_id)` unchanged from Task 6 (fresh retrieve; `active` + item → `ensure_line`; item gone or `canceled` → `release_line`; else `skipped`).
- [ ] Tests: `test_ensure_line_fast_path_records_the_line`, `test_ensure_line_slow_path_returns_pending_and_keeps_the_row`, `test_second_delivery_during_pending_does_not_request_again`, `test_complete_on_read_records_once_across_two_readers` (two callers, one conditional delete wins, both return provisioned, one `put_text_line`), `test_failed_operation_releases_the_claim`, `test_stale_flag_without_rows_is_repaired`, `test_record_failure_releases_the_bought_line`, `test_release_cancels_a_pending_operation`, `test_release_falls_through_when_cancel_loses_the_race`, `test_line_read_completes_pending`.
- [ ] Commit: `front desk: Photon lines via claim, request and completion on read`.

### Task 6: The plan-2 review's Stripe fixes — DONE (2de3c9d6b, 22fe0fc75)

### Task 7: The app: pending state and the hidden button — DONE (1525e9ba3)

### Task 8: Wipes, settings and skill wording

**Files:** `core/services/account_wipe.py` (`release_text_lines` → `photon.release_line(row["photon_resource_id"])` best effort for line rows, `cancel_operation` for `text_pending` rows; the wipe still deletes the rows), `core/config.py` (remove `PHOTON_LINE_POOL_SIZE`), `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md` (the Texts anchor names `routers/photon_inbound.py`), plans 1 to 3 (a one-line note at the top: "provider: Photon 2.0, plan 4").
- [ ] Tests: the two wipe failure tests re-pointed, a pending-row cancel test; commit: `wipes release Photon lines; docs point at Photon`.

### Task 9: Plan 3's bench speaks Photon

A note for plan 3 Task 6: the bench posts Standard-Webhooks-signed `message.received` bodies built from the fixture for `+1555` customers; the dry run is a `space.id` starting `dry:`; the inbox check is unchanged.

### Task 10: Dev acceptance

- [ ] With the dev secrets in place: on a dev trial workspace click "Get a phone number", pay with a test card; the webhook's short poll records the line (fast case) or the panel shows "Setting up your number…" and the line appears on a later read (slow case). Record which, and the wall time.
- [ ] Text the line from a phone: the Mail panel row, the agent's reply in blue, the disclosure once, Take over silencing the agent, the capped autoreply once per day.
- [ ] Register your own phone as a member (Task 11), text the line: the text lands in the main room as you, an agent answers in the room and texts you back; unregister, text again: the front desk answers.
- [ ] Remove the add-on item in the Stripe sandbox dashboard → the line is released; cancel another workspace's subscription → released through `customer.subscription.deleted`. Receipts on the PR.

### Task 11: Members text the team

**Files:** `core/repositories/agent_email_repo.py` (`set_member_phone(number, phone, user_id | None)`: `SET member_phones.#p = :uid` / `REMOVE member_phones.#p` on the line row), `routers/agent_text.py` (Clerk `PUT /me/phone {phone}` (E.164 check as the business phone) and `DELETE /me/phone`; `GET /line` adds `myPhone`), `routers/photon_inbound.py` (the member branch), `core/services/messaging.py` (`via` Literal gains `"text"` at both sites), `apps/frontend/src/components/mail/MailPanel.tsx` (one input under the number: "Text the team from your phone" with Save/Remove), tests.
**Member branch** (inserted after `customer` is known, before `team_address`): `user_id = (line.get("member_phones") or {}).get(customer)`; when set: store the inbox row as above (so the Reply-Key exists), then `messaging.create_human_message(AuthContext(user_id=user_id, org_id=owner_id), default_room_id(owner_id), CreateMessageRequest(client_message_id=uuid5(NAMESPACE_URL, f"lightbulb-text|{object.id}"), text=f"{content.text}\n\n(sent by text from {customer}; to text back: /app/skills/lightbulb/scripts/lightbulb-text.sh send --reply {reply_key})", reply_to_message_id=None, thread_root_message_id=None), via="text")`; `mail_changed`; return `_final("member_routed")`. No stranger caps, no typing, no front-desk delivery. A member's reply by text to the agent's text-back goes the same way (every member text is a room message).
- [ ] Tests: `test_member_text_is_posted_to_the_default_room_as_the_member`, `test_member_text_never_wakes_the_front_desk`, `test_unregistered_phone_is_a_customer_again`, `test_me_phone_requires_e164`, the repo map test, a MailPanel test for the input.
- [ ] Commit: `text agents: a member's text is routed like a room message`.

### Task 12: Delete Sendblue (after Task 10 passes)

**Files:** delete `core/services/sendblue.py`, `routers/sendblue_inbound.py`, their tests; `main.py` unmount; `core/config.py` drop `SENDBLUE_*`; `core/services/text_lines.py` / `account_wipe.py` no Sendblue imports remain (`grep -rn sendblue apps/` is empty except docs).
- [ ] Run the touched test files; commit: `text agents: Sendblue removed`.

## Self-review notes

- Spec coverage: Rules (two kinds of texter: Task 11; the rest as plan 1), Facts (Photon paragraph), Mechanisms Inbound/Outbound/Numbers (no pool, completion on read), Calls (add-on at $250). The plan-2 review's Criticals and Importants: done in Task 6/7 or carried into Task 5 (claim, pending row, release on record failure, cancel-race fall-through).
- The challenge's six changes: fixture from the public spec (done), line lookup by user id as the fallback (Task 3), no stale-lease double buy (one pending row per owner; the request key is fresh per claim and the claim cannot be retaken while the pending row exists), conditional-delete completion (Task 5), no pool (cut), Sendblue kept until acceptance (Task 12).
- Open until the first real calls (Task 0): the signature header names; where the slow case lands in practice (the wall time). Open with Photon (fact sheet §8): Android/SMS on the iMessage line, refund on release, per-line limits.
