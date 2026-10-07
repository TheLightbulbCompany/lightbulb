---
name: shopify
description: Read the owner's connected Shopify store — orders, products, inventory and customers — through the Shopify Admin GraphQL API.
---

# Shopify

The owner connected their store with a Dev Dashboard app (Client ID + secret). Admin API
tokens from such an app last 24 hours, so always get one through `token.sh`, which reuses a
cached token and mints a new one when it is about to expire:

```bash
SHOPIFY_DIR="$HERMES_HOME/skills/shopify"
SHOP=$(jq -r .shop "$SHOPIFY_DIR/credentials.json")
TOKEN=$("$SHOPIFY_DIR/token.sh") || exit 1
```

Run `token.sh` at the start of each task that touches Shopify; it is cheap when cached.

**Never print, echo, log or paste `$TOKEN` or anything from `credentials.json` except
`.shop`** — not in chat, not in files, not in commit messages.

## Querying

All calls are POSTs to the GraphQL Admin API. Build the body with `jq` so quoting is safe:

```bash
gql() {
  curl -sS "https://$SHOP/admin/api/2026-07/graphql.json" \
    -H "X-Shopify-Access-Token: $TOKEN" -H 'Content-Type: application/json' \
    --data "$(jq -n --arg q "$1" '{query: $q}')"
}

gql '{ shop { name currencyCode } }'
gql '{ orders(first: 10, reverse: true) { nodes { name createdAt displayFinancialStatus totalPriceSet { shopMoney { amount currencyCode } } } } }'
```

Paginate with `pageInfo { hasNextPage endCursor }` and `after:`. Check `.errors` in every
response — GraphQL answers 200 even when a field is refused.

## Rules

- **Reads only.** Never run a mutation (anything under `mutation { … }`) unless the owner
  asked for that exact change in this conversation and confirmed it. A live store's orders,
  prices and inventory are real money.
- `ACCESS_DENIED` / a missing-scope error means the app was not granted that scope. Tell the
  owner which scope is missing (e.g. `read_orders`): they add it in the Dev Dashboard under
  the app's version, release it, and reinstall the app on the store — no reconnect needed.
- If `token.sh` fails, the app was uninstalled or its secret rotated: ask the owner to
  reconnect Shopify in Settings → Connections. Do not retry in a loop.
