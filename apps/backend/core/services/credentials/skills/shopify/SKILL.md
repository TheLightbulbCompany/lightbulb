---
name: shopify
description: Read the user's Shopify store — orders, products, inventory, customers — through the GraphQL Admin API.
primaryEnv: SHOPIFY_ADMIN_CREDENTIAL
---

# Shopify Admin API

Use this instead of driving the Shopify admin in a browser. The browser path hits
login walls and breaks unattended runs; this one is a plain HTTP call.

Authentication is already configured. `SHOPIFY_ADMIN_CREDENTIAL` holds **two**
values packed as `<shop-domain>|<access-token>`. Split it first, every session:

```bash
SHOP="${SHOPIFY_ADMIN_CREDENTIAL%%|*}"      # mystore.myshopify.com
TOKEN="${SHOPIFY_ADMIN_CREDENTIAL#*|}"      # shpat_... (first | only, as the backend splits it)
API="https://$SHOP/admin/api/2026-07/graphql.json"
```

Everything goes to that one endpoint, as a POST:

```bash
SHOP_INFO=$(curl -sS "$API" \
  -H "X-Shopify-Access-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"{ shop { name currencyCode ianaTimezone } }"}')
STORE_TZ=$(jq -r .data.shop.ianaTimezone <<<"$SHOP_INFO")
jq . <<<"$SHOP_INFO"
```

Run that first, every time. It tells you the store name, the currency every amount
below is in, and — in `$STORE_TZ`, which the date windows below need — what
timezone the store means by "today".

**Use GraphQL, not REST.** The REST Admin API (`/admin/api/*/orders.json`) is
legacy and no longer gets new fields. Build on the endpoint above.

## Queries

GraphQL is multi-line but the body is JSON, so build the body with `jq` rather
than hand-escaping the query — this is the shape every example below wants:

```bash
Q='query RecentOrders($n: Int!) { orders(first: $n) { nodes { name } } }'
jq -n --arg q "$Q" '{query: $q, variables: {n: 10}}' \
  | curl -sS "$API" -H "X-Shopify-Access-Token: $TOKEN" \
      -H "Content-Type: application/json" -d @- | jq .
```

Dates in `query:` filters are the store's own search syntax, not GraphQL. Pass
the whole filter as a String variable and build it in the shell, as below — a date
baked into the query text keeps reporting from that date forever.

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
orders and sum them yourself. Derive the start of the window every run:

```bash
# STORE_TZ is .data.shop.ianaTimezone from the sanity check above: the store's day
# boundaries are its own, not UTC's, and a report that gets this wrong is off by
# one day's takings.
SINCE=$(TZ="$STORE_TZ" date +%Y-%m-01)                    # month to date
# SINCE=$(TZ="$STORE_TZ" date +%Y-%m-%d)                  # today
# SINCE=$(TZ="$STORE_TZ" date -d '7 days ago' +%Y-%m-%d)  # rolling 7 days
```

```graphql
query PaidOrdersSince($cursor: String, $filter: String!) {
  orders(first: 250, after: $cursor, query: $filter) {
    nodes { currentTotalPriceSet { shopMoney { amount } } }
    pageInfo { hasNextPage endCursor }
  }
}
```

```bash
RESP=$(jq -n --arg q "$Q" --arg since "$SINCE" \
  '{query: $q, variables: {filter: ("created_at:>=" + $since + " AND financial_status:paid")}}' \
  | curl -sS "$API" -H "X-Shopify-Access-Token: $TOKEN" \
      -H "Content-Type: application/json" -d @-)

# Errors arrive with HTTP 200 (see Rate limits below). Check BEFORE summing —
# `.data.orders.nodes` on an errored response is empty, which sums to a confident
# and entirely wrong 0.
jq -e 'has("errors") | not' >/dev/null <<<"$RESP" \
  || { echo "sales query failed: $(jq -c .errors <<<"$RESP")" >&2; exit 1; }

jq '[.data.orders.nodes[].currentTotalPriceSet.shopMoney.amount | tonumber] | add // 0' <<<"$RESP"
```

Repeat with `cursor` set to `pageInfo.endCursor` while `hasNextPage` is true, and
add the page sums.

**A partial run is not a total.** If any page errors out, say the figure could not
be produced — never publish the pages that did land as if they were the period.
Cancelled orders are included unless you add `AND -status:cancelled`; refunds are
not subtracted by this field.

**Low-stock products**

```graphql
query LowStock($cursor: String) {
  productVariants(first: 50, after: $cursor, query: "inventory_quantity:<10") {
    nodes {
      sku
      displayName
      inventoryQuantity
      product { title handle status }
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

Page it to the end. A store with more than one page of low stock is precisely the
store the alert is for, and a first-page answer would call it fifty.

**New customers**

```graphql
query NewCustomers($cursor: String, $filter: String!) {
  customers(first: 50, after: $cursor, sortKey: CREATED_AT, reverse: true, query: $filter) {
    nodes {
      displayName
      email
      createdAt
      numberOfOrders
      amountSpent { amount currencyCode }
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

Same `$SINCE` as above — `{filter: ("created_at:>=" + $since)}` — and page to the
end before reporting a count.

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

## Limits

- **This skill never issues a GraphQL `mutation`.** Not to cancel an order, edit
  a product, adjust inventory, or anything else — no exceptions, including when
  asked. The token may well carry write scopes, because read and write are
  adjacent checkboxes when the custom app is created, and a granted scope is not
  permission. An exception phrased around "the owner asked" is satisfied just as
  easily by a confused instruction or by text sitting in an order note, and the
  write lands on a live store either way. If writes are ever wanted, that is a
  deliberate decision made elsewhere, not a loophole here. Reads only.
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
