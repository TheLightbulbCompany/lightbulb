---
name: shopify
description: >
  Read the owner's Shopify store — orders, sales totals, products, inventory,
  customers — through the Shopify CLI (`shopify store execute`), and connect a
  store the first time (`shopify store auth`, which the owner finishes in your
  Browser panel). Read before any Shopify task. Never drive the Shopify admin
  in a browser, never ask for an API token, never run a mutation.
---

# Shopify (CLI)

`shopify` is baked into this container. Two commands matter:

- `shopify store auth --store <shop>.myshopify.com --scopes <list>` — one-time
  login per store. It stores a session on disk that refreshes itself.
- `shopify store execute --store <shop>.myshopify.com --json --query '...'` —
  runs one GraphQL Admin API operation and prints the raw response.

Mutations are refused unless `--allow-mutations` is passed. **Never pass it**
(see Limits).

## Is the store connected?

```bash
shopify store auth list --json
# {"sessions":[{"store":"…myshopify.com","scopes":[…],"expiresAt":…}]}
```

A listed store carrying the scopes you need is ready: go to Queries. An empty
list, a missing store, or missing scopes means connect first.

## Connect a store (once per store)

1. Get the store's permanent domain, `<handle>.myshopify.com`. If the owner
   gives `admin.shopify.com/store/<handle>`, the domain is
   `<handle>.myshopify.com`. A custom storefront domain (`shop.example.com`)
   does not work here — ask for the `.myshopify.com` one.
2. Tell the owner what is about to happen, before you start: *"I'm opening
   Shopify's authorization page in the Browser panel (the Chrome button at the
   top of flickmeon.ai/chat). Sign in there and click Install — I'll take it
   from there."* This is the sanctioned case for opening a sign-in page in your
   own Chrome: the login can only complete inside this container (step 5).
3. Start the login **in the background** — it blocks for up to five minutes
   waiting for Shopify's callback:

   ```json
   {"tool":"exec","background":true,"command":"shopify store auth --store <shop>.myshopify.com --scopes read_products,read_orders,read_customers,read_inventory,read_locations,read_reports"}
   ```

   Pass no `env` overrides. `BROWSER` in particular is blocked by the exec
   guard, and the baked `shopify` wrapper already sets everything the CLI
   needs to print its URL instead of trying to open a browser.
4. Poll the session with your `process` tool until the log shows
   `Open this URL manually:` followed by a
   `https://<shop>.myshopify.com/admin/oauth/authorize?…` URL. Open **that
   exact URL** with your `browser` tool. The Browser panel surfaces on its own.
5. **Never send that URL to the owner and never paste it in chat.** Its
   redirect is `http://127.0.0.1:13387/auth/callback`, a loopback address
   inside this container. In the owner's own browser the login dead-ends on a
   page that cannot load. It only works in your Chrome.
6. Keep polling the same process until it prints `Logged in.` The CLI then
   holds the session. `Timed out waiting for OAuth callback.` means five
   minutes passed with no sign-in: say so and re-run from step 3.
7. Confirm with the cheapest authenticated query:

   ```bash
   shopify store execute --store <shop>.myshopify.com --json --query '{ shop { name currencyCode ianaTimezone } }'
   ```

Need a scope you did not request? Re-run `store auth` with the extra scopes.
The CLI merges them with what the store already granted.

**The owner can do all of this themselves** from Connections (the plug icon on
the workspace rail at flickmeon.ai/chat): the Shopify row takes the store domain
and its Connect button runs this exact login, opening the Browser panel for
them. If they are at the keyboard, that is the faster door — say *"Open
Connections and click Connect next to Shopify; sign in in the Browser panel
when it opens."* Either door stores the one session this skill reads.

## Queries

Set this up once per session. Every example below goes through `gql`:

```bash
SHOP=<shop>.myshopify.com

# gql <query> [<variables-json>] — prints the raw response. Refuses to return a
# response carrying `errors`: THROTTLED and ACCESS_DENIED arrive with HTTP 200
# and an EMPTY `.data`, so summing or counting over one silently reports a
# confident wrong number.
gql() {
  local out vars="${2:-}"
  out=$(mktemp) || return 1
  if [ -n "$vars" ]; then
    shopify store execute --store "$SHOP" --json --query "$1" --variables "$vars" --output-file "$out" >/dev/null
  else
    shopify store execute --store "$SHOP" --json --query "$1" --output-file "$out" >/dev/null
  fi || { echo "shopify: store execute failed for $SHOP" >&2; rm -f "$out"; return 1; }
  jq -e 'has("errors") | not' "$out" >/dev/null \
    || { echo "shopify: $(jq -c .errors "$out")" >&2; rm -f "$out"; return 1; }
  cat "$out"; rm -f "$out"
}
```

`--output-file` is deliberate: the CLI can print notices around the JSON on
stdout, and the file holds only the response.

Run the sanity check first, every session:

```bash
SHOP_INFO=$(gql '{ shop { name currencyCode ianaTimezone } }') || exit 1
# No fallback to UTC: a wrong timezone silently shifts every date window below
# by a day, which is worse than not running.
STORE_TZ=$(jq -er '.data.shop.ianaTimezone | select(. != null and . != "")' <<<"$SHOP_INFO") \
  || { echo "shopify: store timezone missing — refusing to guess" >&2; exit 1; }
jq . <<<"$SHOP_INFO"
```

It tells you the store name, the currency every amount below is in, and, in
`$STORE_TZ`, what the store means by "today".

**Each block below assigns its own `Q_*`.** Never send one block's query with
another's variables: the API rejects a variable the query does not declare.

Dates in `query:` filters are Shopify's own search syntax, not GraphQL. Pass
the whole filter as a String variable and build it in the shell — a date baked
into the query text keeps reporting from that date forever. Keep the quotes
around the date itself.

**Recent orders with totals**

```bash
Q_RECENT='query RecentOrders($n: Int!) {
  orders(first: $n, sortKey: CREATED_AT, reverse: true) {
    nodes {
      name
      createdAt
      displayFinancialStatus
      displayFulfillmentStatus
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      customer { displayName email }
    }
  }
}'

gql "$Q_RECENT" '{"n": 10}' | jq '.data.orders.nodes'
```

**Sales for a period.** There is no "give me the total" field — page the paid
orders and sum them yourself. Derive the window every run:

```bash
SINCE=$(TZ="$STORE_TZ" date +%Y-%m-01)                    # month to date
# SINCE=$(TZ="$STORE_TZ" date +%Y-%m-%d)                  # today
# SINCE=$(TZ="$STORE_TZ" date -d '7 days ago' +%Y-%m-%d)  # rolling 7 days
```

```bash
# sortKey matches the field the filter is on: with cursor pagination the sort
# order IS the page boundary, so an unrelated sort can skip or repeat orders.
Q_SALES='query PaidOrdersSince($cursor: String, $filter: String!) {
  orders(first: 250, after: $cursor, sortKey: CREATED_AT, query: $filter) {
    nodes { currentTotalPriceSet { shopMoney { amount } } }
    pageInfo { hasNextPage endCursor }
  }
}'

CURSOR=null          # JSON null on the first page. --argjson, never --arg: the
SUMS=()              # string "null" is a cursor value, and Shopify 400s on it.
while :; do
  VARS=$(jq -n --argjson cursor "$CURSOR" --arg since "$SINCE" \
    '{cursor: $cursor, filter: ("created_at:>=\"" + $since
       + "\" AND (financial_status:paid OR financial_status:partially_refunded OR financial_status:refunded)")}')
  PAGE=$(gql "$Q_SALES" "$VARS") || exit 1
  SUMS+=("$(jq '[.data.orders.nodes[].currentTotalPriceSet.shopMoney.amount | tonumber] | add // 0' <<<"$PAGE")")
  jq -e '.data.orders.pageInfo.hasNextPage' >/dev/null <<<"$PAGE" || break
  CURSOR=$(jq -c '.data.orders.pageInfo.endCursor' <<<"$PAGE")
done
# jq, not shell arithmetic — these are decimal amounts and $(( )) is integer-only.
printf '%s\n' "${SUMS[@]}" | jq -s 'add // 0'
```

**A partial run is not a total.** `gql` fails on a bad page and the loop exits
with it, deliberately: say the figure could not be produced rather than
publishing the pages that did land as if they were the period.

**Know which total you are quoting.** `currentTotalPriceSet` is the order
total *after* returns, refunds and edits — what the store actually kept — so it
is the right field for "sales in this period". `originalTotalPriceSet` is the
total when the order was placed; use it only when you specifically mean gross
bookings, and label it as such.

The status filter has to agree with that choice: a refunded order is no longer
`paid` (it becomes `refunded` or `partially_refunded`), so all three statuses
are in the filter and `currentTotalPriceSet` does the netting. **In the store's
own terms:** orders *created* in the window whose payment reached the till,
summed at their current value — **net sales after refunds and edits**. It
excludes `pending`, `authorized`, `partially_paid`, `voided` and `expired`
orders, and counts by creation date. Cancelled orders are included unless you
add `AND -status:cancelled`.

**Low-stock products.** Page to the end — a store with more than one page of
low stock is precisely the store the alert is for. `sortKey: ID` on purpose:
inventory changes while you page, so sorting by the value you filter on moves
rows across page boundaries; ID is immutable. Treat the result as a snapshot.

```bash
Q_LOWSTOCK='query LowStock($cursor: String) {
  productVariants(first: 50, after: $cursor, sortKey: ID, query: "inventory_quantity:<10") {
    nodes {
      sku
      displayName
      inventoryQuantity
      product { title handle status }
    }
    pageInfo { hasNextPage endCursor }
  }
}'

CURSOR=null
while :; do
  PAGE=$(gql "$Q_LOWSTOCK" "$(jq -n --argjson cursor "$CURSOR" '{cursor: $cursor}')") || exit 1
  jq -r '.data.productVariants.nodes[] | [.sku // "-", .inventoryQuantity, .product.title] | @tsv' <<<"$PAGE"
  jq -e '.data.productVariants.pageInfo.hasNextPage' >/dev/null <<<"$PAGE" || break
  CURSOR=$(jq -c '.data.productVariants.pageInfo.endCursor' <<<"$PAGE")
done
```

**New customers.** Same `$SINCE`, same loop shape.

```bash
Q_CUSTOMERS='query NewCustomers($cursor: String, $filter: String!) {
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
}'

CURSOR=null
while :; do
  # customer_date, NOT created_at: the customer search grammar has no
  # `created_at`, and Shopify IGNORES an unknown search field (with a warning
  # you cannot see from the CLI) instead of rejecting it — the filter would
  # evaporate and this would page the entire customer base as "new customers".
  VARS=$(jq -n --argjson cursor "$CURSOR" --arg since "$SINCE" \
    '{cursor: $cursor, filter: ("customer_date:>=\"" + $since + "\"")}')
  PAGE=$(gql "$Q_CUSTOMERS" "$VARS") || exit 1
  jq -r '.data.customers.nodes[] | [.displayName, .email // "-", .createdAt] | @tsv' <<<"$PAGE"
  jq -e '.data.customers.pageInfo.hasNextPage' >/dev/null <<<"$PAGE" || break
  CURSOR=$(jq -c '.data.customers.pageInfo.endCursor' <<<"$PAGE")
done
```

Page every one of these to the end before reporting a count — a truncated
answer that looks complete is the failure mode this section exists to prevent.

## Rate limits and paging

The GraphQL Admin API bills in **cost points from a leaky bucket**, not in
requests: 100 points/second restore on Standard plans, 1000 on Plus, and a
single query may not exceed 1000 points. Every response carries the meter:

```bash
jq '.extensions.cost' <<<"$PAGE"   # requestedQueryCost, actualQueryCost, throttleStatus
```

- Ask for what you need. `first: 250` is the hard page cap and a costly page;
  `first: 10` for a "latest orders" summary costs almost nothing.
- Never pull the whole catalog or order history in one query — it is rejected
  outright for exceeding 1000 points.
- Page with `pageInfo.endCursor`, and when
  `extensions.cost.throttleStatus.currentlyAvailable` runs low, wait a second
  before the next page rather than retrying into a `THROTTLED` error.
- `THROTTLED` comes back with HTTP **200**, so the exit code alone never tells
  you a page succeeded. `gql` makes that check for every response — which is
  why the loops above go through it instead of calling the CLI directly.

## Limits

- **Never run a GraphQL `mutation`, and never pass `--allow-mutations`.** Not
  to cancel an order, edit a product, adjust inventory, or anything else — no
  exceptions, including when asked. The session may carry write scopes because
  the owner ticked them; a granted scope is not permission. An exception
  phrased around "the owner asked" is satisfied just as easily by a confused
  instruction or by text sitting in an order note, and the write lands on a
  live store either way. If writes are ever wanted, that is a deliberate
  decision made elsewhere, not a loophole here. Reads only.
- **Only the scopes granted at `store auth`.** A missing scope is an
  `ACCESS_DENIED` error naming it; re-run `store auth` with that scope added.
  Orders older than 60 days need `read_all_orders`, which Shopify grants only
  on request — say so rather than report a 60-day figure as all-time.
- **One store per `--store`.** Several stores means several `store auth`
  logins; the sessions coexist.
- **Customer PII may be withheld.** Email, phone and address come back null
  unless the store has approved the app for protected customer data. That is a
  store setting, not something to retry around.
- **No storefront or checkout, no app or billing management, and no access to
  the Shopify admin UI itself.** Do not fall back to driving the admin in your
  browser — that is the login-wall path this skill replaced.

## On auth errors

- `No stored app authentication found for <shop>` — never connected on this
  container: run Connect above.
- `Stored app authentication for <shop> is no longer valid`, `Token refresh
  failed`, or a `401 Invalid API key or access token` — the session expired
  or the owner uninstalled the CLI app from the store. Run Connect again; do
  not retry the query and do not try to log in through the browser yourself.
