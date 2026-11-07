# Money: the ledger, the company Stripe account, and paid services

## Where the owner sees the money

Once the company Stripe account is connected, the owner has one **Money** tab
on **Work** (the workspace rail's Work button) — a ledger page. Point them at it rather than
describing numbers you would have to fetch yourself. Top to bottom:

- the masthead says what landed this month, and the biggest day links to that
  day's payments;
- **Payments** — every charge, newest first, with its Stripe receipt, so it
  reconciles against their Stripe Dashboard line for line;
- **Customers** — who bought, when, and their balance, with how many are new
  in the last 30 days;
- **What you sell** — their products and prices, and a link to add one in
  Stripe;
- **Agent spend** — what the agents cost this period: the billed figure, runs,
  and the split by model (or the ChatGPT plan windows). The token detail and
  the per-member table live in Settings → Usage.

There is no Home page any more. For "how are we doing" and "show me the
charge" point them at Money; for "what needs me", Activity.

Two things worth saying accurately if asked:

- Money reads the owner's connected Stripe account **through**; nothing is
  copied or cached, so what they show is what Stripe says.
- Before onboarding is finished the page says so explicitly ("Your Stripe
  onboarding isn't finished" / "Stripe is still reviewing your account") rather
  than showing an empty table. If the owner reports one of those, the answer is
  to finish setup in Settings → Connections, not to wait.

**Only workspace admins can open Money.** An ordinary org member gets
a permission message, not an error — do not tell them to retry.

## Budgets: monthly caps on agent spend

A workspace admin can set a **monthly budget** on an agent (or on the whole
company) at **Settings → Budgets** — a dollar ceiling on managed inference
spend. Spend accrues there from the real per-call cost, so the figure is
actual, not an estimate. At 80% the owner is warned; at the ceiling the agent
is **paused** until the month rolls over or the owner raises the cap. Any
member can see the budgets; only an admin can set them. If an agent reports
being paused for budget, the answer is Settings → Budgets, not a retry. These
caps are the soft, owner-facing layer — the hard real-time spend stop is the
platform's own managed-credit limit, which is separate and always in force.

## Buying things (`request_purchase`)

You never hold a card. For a real purchase your task needs, call `request_purchase` (merchant, url,
amount in dollars, at most $500, why, issue_id): it returns `{requestId, status}`. Normally the owner
approves it in Lightbulb and then in Link (`needs_owner`). If the owner turned off "ask before moving
money" and trusts that merchant, the Link request is created at once (`pending_approval`) and only
Link's approval remains, within 30 minutes or it expires; limits are the owner's (Settings -> Budgets -> Purchases). A refusal
code (`over_purchase_cap`, `over_project_cap`, `no_limits`, `wallet_disconnected`) means tell the
owner, never retry with a smaller split. Retrying the same ask returns the same request.

## Your company's Stripe account

Money for the business (selling a product, taking payments, paying for a
provider) needs the company's own Stripe account. Check `$HERMES_HOME/lightbulb-facts.json`
for `company_stripe: "connected"`; if it is absent or `"none"`, do not
improvise a Stripe signup — ask the owner through the platform. `"pending"`
means the account exists but onboarding is not finished: do not file the
request again, and do not treat it as connected — say it is still being set
up.

```bash
/app/skills/lightbulb/scripts/lightbulb-api.sh POST \
  "/api/v1/company/stripe/u/$ISOL8_OWNER_ID/request" \
  '{"issue_id":"'"$PAPERCLIP_TASK_ID"'","reason":"<one sentence on why>"}'
```

The last line is the HTTP status — 200 filed/connected/no_company, 503
unreachable (retry next turn), 502 rejected (tell the owner).

If the call returns 503, the board was unreachable — say so to the owner and
try again on your next turn; never work around it. A 502 is different: the
board REJECTED the request, so retrying changes nothing — tell the owner what
it said, verbatim, and move on.

A 200 is not always a card. Read `status` in the body: `filed` means a
"Set up <company> on Stripe" card is now in the owner's Activity (For you) — say in your
reply that you asked; `connected` means the company already has its Stripe
account and there is nothing to ask for; `no_company` means the owner has no
board yet (the platform is still provisioning it) — tell the owner plainly
and try again on a later turn. The card is never a gate: keep working on
everything that does not need money. The owner keeps the account and 100% of
the revenue; Lightbulb AI takes no fee. Never create the Stripe account in
the browser yourself.

## A paid service the company does not have

Lightbulb cannot create provider accounts (analytics, hosting, a database…) for
the company. When a task needs one, say so, recommend one specific provider with
the reason, and ask the owner to create the account and either connect it in
Connections or give you the credential through the `secrets` tool. Prefer a
provider the Connections screen lists (`references/integrations.md`), so it can
be connected afterwards; only if none fits, one well-known provider, labelled as
your own suggestion. Never sign up on their behalf and never invent a credential.
Never print a credential value anywhere: **the name, never the value.**
