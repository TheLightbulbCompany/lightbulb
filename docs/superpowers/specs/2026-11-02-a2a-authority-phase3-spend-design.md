# A2A authority extension, phase 3: spend mandates

**Status:** draft for the founder's approval. Nothing is built.
**Builds on:** phase 1 (PR #4562: the authority object, rules, holds, exceptions, signed records). This branch (`feat/a2a-phase3`) stacks on `feat/a2a-authority-design`; it does not need phase 1b or phase 2. Rebased onto `main` when #4562 merges.
**Builder check (2 Nov):** run; ten questions and seven contradictions folded in; calls marked "my call".
**Scope (the founder's phase list):** Link caps become mandates specific to amount and payee. The caps were located first (goal instruction): `apps/backend/core/services/purchases.py:191 _policy`, limits in `billing_repo.py:1560-1590`.

## 1. What exists, from the code

- Link here is Stripe Link's Agent Wallet, reached by OAuth (`core/services/link_wallet.py`); the wallet is the connecting admin's own Link account. There is no card, no Issuing, and no hook at the moment money moves: Link's own in-app approval is the final gate.
- An agent can only ask. The Hermes tool `request_purchase` (`plugins/lightbulb/__init__.py:242`) posts `{agentId, merchant, url, amount, why, issueId}` to `POST /api/v1/internal/link-wallet/{owner}/requests` (hook token). `purchases.request_purchase` (`purchases.py:254`) sanitizes, normalizes the URL to a `site`, hashes an idempotent id, then under the owner lease runs `_policy(limits, cents, project_id, site, rows)` (`:191`): refuses `no_limits`, `over_purchase_cap` (per-purchase cents clamped to `MAX_AMOUNT_CENTS` = 50 000) or `over_project_cap` (monthly per project). It stores a ledger row `PURCHASE#<id>` with `status needs_owner`, files a board card, and when the site is on the workspace `merchants` allowlist and `money_moves` is off, creates the Link spend request at once. The owner's click (`approval_link`, `:353`) re-runs `_policy`, creates the Link request, and returns Link's `approval_url`.
- Limits are per workspace: `perPurchaseCents`, `projectMonthlyCents`, `merchants[]` (`billing_repo.py:1560`), set by an org admin (`company_link_wallet.py:101`).
- The payee is identified by `site` (the URL's registrable origin) plus free-text `merchant`. Currency is hard-coded `usd`. The ledger row records no approver identity and no signed record; the approver is whoever clicks in Link.
- `company_policy.money_moves` is "deliberately NOT enforced inside any rail" (`routers/company_policy.py:4-7`); its only reader is the allowlist shortcut above.

## 2. Intent

Every purchase request becomes an authority object decided by the workspace rules, and a holder's approval becomes a mandate: a signed, time-boxed exception that names the payee and an amount ceiling, so the same shape payment law already requires (approval specific to the amount and the payee) and the shape AP2 ships for payments. The workspace caps stay as outer bounds. The ledger row and the Link spend request carry the authority id and the record, so "who approved this agent's purchase, for whom, up to how much, until when" is answerable from the record, not from a click in another app.

## 3. Goals, non-goals, anti-goals

Goals: a purchase request is minted, decided, held and recorded like any other A2A task; a holder's allow writes a mandate (payee site, ceiling, expiry); a request inside a live mandate is allowed without a hold; every ledger row names the authority and the record; Link metadata carries both.
Non-goals: moving money without Link's approval step; Stripe Issuing or charge-time decisioning; per-agent budgets (Paperclip's inference budgets are a different rail); new agent-facing arguments on the Hermes tool (the tool body gains `roomId` and `originMessageId` from the turn context, never from the model, as `ask_session` does; `PurchaseBody` gains the same two fields; a named origin that fails verification is refused `bad_origin`).
Anti-goals: a mandate that widens beyond its payee or ceiling; a purchase recorded without its approver; a refusal that loses the request silently.

## 4. The purchase as an authority object

`_decide_and_store` is refactored to take an `on_allow` callable (my call, builder-check question 3): the messaging callers pass the DM dispatch; the purchase caller passes "create the Link spend request". A purchase task has no `working`-to-`undeliverable` clock; its state follows the ledger: `allow` → Link request created, task `working`, ledger `pending_approval`; Link `succeeded`/`approved` → task `completed`; a Link dead status → task `failed` with that reason. Inputs: `skill = "purchase"`, `resource = f"wallet:{owner_id}"`, `operands = {"amount_cents", "currency": "usd", "site", "merchant", "url", "issue_id"}`, `intent = why`, requester = the origin person resolved from the turn's trigger message (`verified_by = "origin-message"`), else the stand-in `agent:<agent_id>` with `verified_by = "agent-runtime"` (my call, question 8: an agent-runtime purchase is keyed to the agent, not to the holder, so one mandate never covers every agent). Chain `[origin, agent]`. The idempotent ledger id (`request_id_for`) gains the requester and the agent, and the Link idempotency key follows it.

The workspace caps run first, unchanged, as a hard bound, and again at decide time under the owner lease `_serialized` (question 5): a request over `perPurchaseCents` (clamped to 50 000) or over the project month is refused before any decision, the task is `rejected` with rule `over_purchase_cap` / `over_project_cap` / `no_limits`, and the ledger gets a row with the new status `refused` (today a cap refusal writes no row; `denied` stays Link's own word). Held rows are not counted toward the month until allowed.

Holders of `wallet:{owner}`: the connecting admin whose Link account it is (`link_wallet.connection`), injected at decide time when the stored rules document has no `wallet:*` key (question 4); a document may name others. The phase-1 rule that a verified person requesting a resource they hold is allowed (`holder`) applies: the wallet's own admin asking their own wallet is allowed, and still approves in Link.

## 5. Mandates

A mandate is its own row, not phase 1's exact-operands exception (contradiction: `get_exception` keys on the operands hash with a fixed TTL): `a2a_repo.put_mandate` / `get_mandate` / `debit_mandate`, key `MANDATE#wallet:{owner}#{requester}#{site}`, fields `{mandate_id, ceiling_cents, remaining_cents, currency, holder, task_id, issued_at, expires_at}`, TTL = `expires_at`. The ceiling is a running total (question 1): a request matches when `site` and `currency` equal and `amount_cents <= remaining_cents`, and `debit_mandate` subtracts atomically under the owner lease when the Link request is created (a conditional update on `remaining_cents >= amount`). The caller passes the match to `decide` as `exception={"id": mandate_id, "kind": "mandate"}` and `decide` emits rule `mandate:<id>` (one-line change to `a2a_rules.decide`). Deny rules still win. A mandate's ceiling is capped at `perPurchaseCents` when written and `perPurchaseCents` is re-checked at match time (question 9). A later allow on the same key replaces the earlier mandate (question 7, my call: the holder's newest decision wins; the old remainder is forfeited and recorded).

The holder sets the ceiling and expiry when answering a hold: the decide route's `Decision` body gains `ceiling_cents: int | None` and `expires_in_s: int | None` (max 30 days); defaults are this amount and 24 h. The DM notification (phase 1 §7) and the board card show amount, merchant, site, reason and the chain; "Always allow" is not offered for purchases (my call: a standing allow for money is the widening the anti-goal names).

## 6. Decision outcomes and the Link step

- `allow` (rule, allowlist or mandate): create the Link spend request at once under the lease, with `metadata` extended by `authority_id`, `record_sha256` (the latest signed record's hash; the JWS itself exceeds metadata value limits, the record is fetchable from `/record`) and `mandate_id`; ledger `pending_approval`; the owner still approves in Link.
- `hold`: ledger `needs_owner`; the board card and the DM notification both say what is asked. One authoritative answer path (question 2): the card's buttons and the owner's "Approve in Link" click call `a2a_tasks.decide`; `approval_link` and `cancel_for_card` no longer act on their own. The holder's allow writes the mandate and creates the Link request under the lease; deny → task `rejected`, ledger `declined` (holder's word, distinct from Link's `denied`).
- `deny` / caps: task `rejected`, ledger `refused` with the rule.
- `money_moves` is retired as a reader: the rules document decides. The `merchants` allowlist stays as a built-in input to the purchase branch, not a rule (phase 1 rules have no `when`): a site on the list is `allow` with rule `allowlist` (same effect as today's `eager`, now recorded). Phase 1b's `when` clauses can replace it.
- Link's own approval step stays in every path: `request_approval` is hard-coded true in `link_wallet.create_spend_request` and no false path exists in Link's documented API as read from the code; §10 asks whether to pursue it later.

## 7. Records

Ledger rows gain `authorityId`, `recordSha256` (latest), `mandateId`, `requesterPerson`, `approvedBy`, and the statuses `refused` and `declined`. The signed record decisions reuse phase 1's set plus `allow_by_mandate` and `mandate_replaced`. The replay cases gain three purchase cases (inside mandate, over remaining, wrong site).

## 8. Tests (acceptance)

1. A purchase over the workspace cap is refused before decision, with a `rejected` record naming the cap.
2. With no mandate and default rules, a purchase is held for the wallet holder; the notification carries amount, merchant and site.
3. The holder allows with a ceiling and expiry: a mandate row exists; the Link request is created with the authority id, record and mandate id in metadata; the ledger row names the approver.
4. A second request within the remaining balance to the same site is allowed by `mandate:<id>` without a hold and the balance is debited; a third that exceeds the remaining balance is held; another site is held; after expiry it is held; two concurrent requests cannot both debit the last balance.
5. A deny rule on `wallet:*` refuses even with a mandate.
6. A held purchase answered later re-runs the caps under the lease and is refused if the month filled up meanwhile. The card's approve and the old `approval_link` path cannot create a Link request without a decision.
7. The existing `tests/unit/services/test_link_wallet.py` suite passes with the new fields.

## 9. Left to the builder

The `on_allow` refactor's exact signature; the holder lookup from `link_wallet.connection`; what the Hermes tool returns when held (today `{requestId, status}`; add `taskId`); the board card payload additions; the Link status poll or webhook that moves a task to `completed`; confirming the Hermes `agentId` is the roster agent id used by `_notify_holders`.

## 10. Questions for the founder

1. Link's in-app approval stays in every path, so a purchase inside a mandate still needs the click in Link. Keep it (recommended until a partner asks), or ask Link to skip it when a mandate covers the request?
2. The wallet holder is the connecting admin whose Link account it is. Confirm, or name the workspace admin instead.
3. `money_moves` is retired as a reader in favour of the rules document; the UI key can stay. Confirm.
