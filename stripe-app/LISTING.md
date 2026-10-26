# Listing copy, v0.0.5 (dashboard-only; paste into the Stripe Apps listing)

Answers the 2026-09-16 rejection of v0.0.4. Permissions below are the six in
`stripe-app.json`: `connected_account_read`, `charge_read`, `customer_read`,
`product_read`, `plan_read` (prices), `balance_read`. Change copy and manifest together.

**Subtitle** (62/80)
Create a read-only Stripe key for Lightbulb revenue reporting.

**About** (462/1000)
Lightbulb is a workspace where people and AI agents work together. This app lets you create a Stripe restricted API key that can only read data, which you then paste into Lightbulb (Settings, Connections). With that key, Lightbulb can read your account name, charges, customers, products, prices, and balance to show revenue summaries and briefings. It cannot create charges, refunds, or other changes. You can revoke the key in the Stripe Dashboard at any time.

**Key feature 1 title** (35/80)
Secure, read-only restricted access

**Key feature 1 description** (201/300)
Installing the app generates a restricted API key scoped to six read permissions (account, charges, customers, products, prices, balance), so you do not need to copy and share a full-access secret key.
