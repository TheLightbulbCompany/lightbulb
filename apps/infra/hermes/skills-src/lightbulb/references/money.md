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
amount in dollars, at most $500, why; it is tied to your current task): it returns
`{requestId, status}`. Normally the call first waits for a person's yes on the task's Needs-you
question; then the Link request is created (`pending_approval`). If the owner turned off "ask before
moving money" and trusts that merchant, no question is asked. Either way Link's own approval remains,
within 30 minutes or it expires; limits are the owner's (Settings -> Budgets -> Purchases). A refusal
code (`over_purchase_cap`, `over_project_cap`, `no_limits`, `wallet_disconnected`) means tell the
owner, never retry with a smaller split. Retrying the same ask returns the same request.

## A paid service the company does not have

Lightbulb cannot create provider accounts (analytics, hosting, a database…) for
the company. When a task needs one, say so, recommend one specific provider with
the reason, and ask the owner to create the account and either connect it in
Connections or give you the credential through the `secrets` tool. Prefer a
provider the Connections screen lists (`references/integrations.md`), so it can
be connected afterwards; only if none fits, one well-known provider, labelled as
your own suggestion. Never sign up on their behalf and never invent a credential.
Never print a credential value anywhere: **the name, never the value.**
