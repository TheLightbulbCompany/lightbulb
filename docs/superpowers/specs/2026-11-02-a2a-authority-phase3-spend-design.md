# A2A authority extension, phase 3: spend mandates

**Status:** draft for the founder's approval. Nothing is built.
**Builds on:** phase 1 (PR #4562: the authority object, rules, holds, exceptions, signed records). This branch (`feat/a2a-phase3`) stacks on `feat/a2a-authority-design`; it does not need phase 1b or phase 2. Rebased onto `main` when #4562 merges.
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
Non-goals: moving money without Link's approval step; Stripe Issuing or charge-time decisioning; per-agent budgets (Paperclip's inference budgets are a different rail); changing the Hermes tool's arguments beyond adding nothing (the person comes from the turn context as in phase 1).
Anti-goals: a mandate that widens beyond its payee or ceiling; a purchase recorded without its approver; a refusal that loses the request silently.

## 4. The purchase as an authority object

`request_purchase` becomes a caller of phase 1's `_decide_and_store` with: `skill = "purchase"`, `resource = f"wallet:{owner_id}"`, `operands = {"amount_cents", "currency": "usd", "site", "merchant", "url", "issue_id"}`, `intent = why`, requester = the origin person resolved from the turn's trigger message as `ask_session` does (`verified_by = "origin-message"`), else the wallet holder as stand-in (`verified_by = "agent-runtime"`), chain `[origin, agent]`. The workspace caps run first, unchanged, as a hard bound: a request over `perPurchaseCents` (clamped to 50 000) or the project month is refused before any decision, recorded as `rejected` with rule `over_purchase_cap` / `over_project_cap` / `no_limits`.

The holder of `wallet:*` is the connecting admin whose Link account it is (`link_wallet.connection` names them), my call; the rules document may name others.

## 5. Mandates

A mandate is the exception row of phase 1 with two changes: it binds a payee and a ceiling instead of an exact operands hash, and it carries an expiry chosen by the holder. Row key `EXC#wallet:{owner}#purchase#{requester}#{site}`, fields `{ceiling_cents, currency, holder, task_id, issued_at, expires_at, mandate_id}`, TTL = `expires_at`. A request matches a mandate when `site` equals, `currency` equals and `amount_cents <= ceiling_cents`; it is then `allow` with rule `mandate:<id>`. The holder sets the ceiling and expiry when answering a hold (`verdict allow` with `{"ceiling_cents", "expires_in_s"}`; defaults: this amount, 24 h). Deny rules in the document still win over any mandate. A mandate never exceeds `perPurchaseCents`.

The decide route and the DM notification (phase 1 §7) show amount, merchant, site, reason and the chain; "Always allow" is not offered for purchases (my call: a standing allow for money is exactly the widening the anti-goal names).

## 6. Decision outcomes and the Link step

- `allow` (rule or mandate): create the Link spend request at once with `metadata` extended by `authority_id`, `record` (the latest signed record JWS) and `mandate_id`; ledger status `pending_approval`; the owner still approves in Link.
- `hold`: ledger status `needs_owner` as today; the board card and the DM notification both say what is asked; the holder's allow writes the mandate and creates the Link request; deny → `rejected` + `denied` in the ledger.
- `deny` / caps: `rejected`, ledger `denied` with the rule.
- `money_moves` is retired as a reader: the rules document decides. The `merchants` allowlist becomes a seed rule (`allow purchase on wallet:* for requesters org:<id> when site in merchants`) migrated once by the builder when the document is first created; the field stays for the admin UI.
- Link's own approval step stays in every path in this phase (my call: Link's `request_approval` is left true until a design partner asks for mandate-only flows; §10 asks the founder).

## 7. Records

Ledger rows gain `authorityId`, `recordJws` (latest), `mandateId`, `requesterPerson`, `approvedBy`. The signed record decisions reuse phase 1's set plus `allow_by_mandate`. The replay cases gain three purchase cases (inside mandate, over ceiling, wrong site).

## 8. Tests (acceptance)

1. A purchase over the workspace cap is refused before decision, with a `rejected` record naming the cap.
2. With no mandate and default rules, a purchase is held for the wallet holder; the notification carries amount, merchant and site.
3. The holder allows with a ceiling and expiry: a mandate row exists; the Link request is created with the authority id, record and mandate id in metadata; the ledger row names the approver.
4. A second request under the ceiling to the same site is allowed by `mandate:<id>` without a hold; over the ceiling, or to another site, it is held; after expiry it is held.
5. A deny rule on `wallet:*` refuses even with a mandate.
6. The existing `tests/unit/services/test_link_wallet.py` suite passes with the new fields.

## 9. Left to the builder

The exact mapping of ledger statuses to task states; the migration of `merchants` into a seed rule; the holder lookup from `link_wallet.connection`; what the Hermes tool returns when held (today `{requestId, status}`); the board card payload additions.

## 10. Questions for the founder

1. Link's in-app approval stays in every path, so a purchase inside a mandate still needs the click in Link. Keep it (recommended until a partner asks), or ask Link to skip it when a mandate covers the request?
2. The wallet holder is the connecting admin whose Link account it is. Confirm, or name the workspace admin instead.
3. `money_moves` is retired as a reader in favour of the rules document; the UI key can stay. Confirm.
