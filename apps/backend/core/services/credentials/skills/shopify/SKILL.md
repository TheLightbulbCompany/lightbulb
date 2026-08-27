---
name: shopify
description: Read and manage the user's Shopify store through the GraphQL Admin API.
primaryEnv: SHOPIFY_ADMIN_CREDENTIAL
---

# Shopify Admin API

Use this instead of driving the Shopify admin in a browser. The browser path hits
login walls and breaks unattended runs; this one is a plain HTTP call.

Authentication is already configured. `SHOPIFY_ADMIN_CREDENTIAL` holds **two**
values packed as `<shop-domain>|<access-token>`. Split it first, every session:

```bash
SHOP="${SHOPIFY_ADMIN_CREDENTIAL%%|*}"      # mystore.myshopify.com
TOKEN="${SHOPIFY_ADMIN_CREDENTIAL##*|}"     # shpat_...
API="https://$SHOP/admin/api/2026-07/graphql.json"
```

Everything goes to that one endpoint, as a POST:

```bash
curl -sS "$API" \
  -H "X-Shopify-Access-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"{ shop { name currencyCode ianaTimezone } }"}' | jq .
```

Run that first when a store's answers look wrong — it tells you the store name,
the currency every amount below is in, and the timezone "today" means.

**Use GraphQL, not REST.** The REST Admin API (`/admin/api/*/orders.json`) is
legacy and no longer gets new fields. Build on the endpoint above.

## Queries

Pass variables in the same JSON body (`{"query": "...", "variables": {...}}`).
Dates in `query:` filters are the store's own search syntax, not GraphQL — quote
them exactly as shown.

**Recent orders with totals**

```graphql
query RecentOrders($n: Int!) {
  orders(first: $n, sortKey: CREATED_AT, reverse: true) {
    nodes {
      name
      createdAt
      displayFinancialStatus
      displayFulfillmentStatus
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      customer { displayName email }
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

**Sales for a period.** There is no "give me the total" field — page the paid
orders and sum them yourself:

```graphql
query PaidOrdersSince($cursor: String) {
  orders(
    first: 250
    after: $cursor
    query: "created_at:>=2026-08-01 AND financial_status:paid"
  ) {
    nodes { currentTotalPriceSet { shopMoney { amount } } }
    pageInfo { hasNextPage endCursor }
  }
}
```

```bash
jq '[.data.orders.nodes[].currentTotalPriceSet.shopMoney.amount | tonumber] | add'
```

Repeat with `cursor` set to `pageInfo.endCursor` while `hasNextPage` is true, and
add the page sums. Cancelled orders are included unless you add
`AND -status:cancelled`; refunds are not subtracted by this field.

**Low-stock products**

```graphql
query LowStock {
  productVariants(first: 50, query: "inventory_quantity:<10") {
    nodes {
      sku
      displayName
      inventoryQuantity
      product { title handle status }
    }
  }
}
```

**New customers**

```graphql
query NewCustomers {
  customers(
    first: 50
    sortKey: CREATED_AT
    reverse: true
    query: "created_at:>=2026-08-01"
  ) {
    nodes {
      displayName
      email
      createdAt
      numberOfOrders
      amountSpent { amount currencyCode }
    }
  }
}
```

## Rate limits and paging

The GraphQL Admin API bills in **cost points from a leaky bucket**, not in
requests: 100 points/second restore on Standard plans, 1000 on Plus, and a single
query may not exceed 1000 points. Every response carries the meter:

```bash
jq '.extensions.cost'   # requestedQueryCost, actualQueryCost, throttleStatus
```

So:

- Ask for what you need. `first: 250` is the hard page cap and a costly page;
  `first: 10` for a "latest orders" summary costs almost nothing.
- Never try to pull the whole catalog or order history in one query — it will be
  rejected outright for exceeding 1000 points.
- Page with `pageInfo.endCursor`, and when
  `extensions.cost.throttleStatus.currentlyAvailable` runs low, wait a second
  before the next page rather than retrying into a `THROTTLED` error.
- A `THROTTLED` error in `errors[]` comes back with HTTP **200**. Check
  `.errors` on every response; do not trust the status code alone.

## What this token cannot do

- **One store.** It is scoped to `$SHOP` and cannot reach any other store.
- **Only the scopes the owner ticked** when they created the custom app. A
  missing scope is an `ACCESS_DENIED` error naming the scope — tell the owner
  which one to add in Settings → Apps and sales channels → Develop apps, and that
  they must reinstall the app for it to take effect.
- **Customer PII may be withheld.** Email, phone and address come back null
  unless the app is approved for protected customer data. That is a store setting,
  not something to retry around.
- **No storefront or checkout.** It cannot browse as a shopper, build carts, or
  complete checkouts — those are different APIs with different credentials.
- **No app or billing management**, and no access to the Shopify admin UI itself.

## On auth errors

A `401` with "Invalid API key or access token" means the custom app was
uninstalled or its token rotated. Tell the user to reconnect Shopify in Settings →
Integrations; do not retry, and do not try to log in through the browser.
