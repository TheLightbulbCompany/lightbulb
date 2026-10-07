#!/bin/bash
# Print a Shopify Admin API access token for the connected store.
#
# Dev Dashboard apps have no long-lived token: a token is minted with the client-credentials
# grant and lives 24 h. This reuses the cached token until 5 minutes before it expires, then
# mints a new one — so a session mints at most once a day. The cache is keyed on shop +
# client_id, so a reconnect to a different store/app never reuses the old store's token.
#
# Never prints the client secret. The secret reaches curl on STDIN, not argv, so it is not
# visible in a process listing.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CRED="$DIR/credentials.json"
CACHE="$DIR/token-cache.json"

if [ ! -r "$CRED" ]; then
  echo "shopify: not connected — connect Shopify in Settings → Connections" >&2
  exit 1
fi
shop=$(jq -r '.shop // empty' "$CRED" 2>/dev/null)
cid=$(jq -r '.client_id // empty' "$CRED" 2>/dev/null)
now=$(date +%s)

if [ -r "$CACHE" ] && jq -e --arg s "$shop" --arg c "$cid" --argjson min $((now + 300)) \
     '.shop == $s and .client_id == $c and (.expires_at // 0) > $min and ((.access_token // "") | length) > 0' \
     "$CACHE" >/dev/null 2>&1; then
  jq -r '.access_token' "$CACHE"
  exit 0
fi

response=$(jq -r '"grant_type=client_credentials&client_id=\(.client_id | @uri)&client_secret=\(.client_secret | @uri)"' "$CRED" |
  curl -sS --max-time 15 -X POST "https://$shop/admin/oauth/access_token" \
    -H 'Content-Type: application/x-www-form-urlencoded' --data-binary @- 2>/dev/null) || response=""
token=$(printf '%s' "$response" | jq -r '.access_token // empty' 2>/dev/null)
if [ -z "$token" ]; then
  echo "shopify: could not mint a token for $shop — check the app is still installed on the store and its client secret is current, then reconnect in Settings → Connections" >&2
  exit 1
fi
ttl=$(printf '%s' "$response" | jq -r '.expires_in // 86399' 2>/dev/null)
ttl=${ttl//[^0-9]/}

# Same-dir temp + mv, so a crash mid-write never leaves a torn cache. A failed cache write
# is not fatal: the token is still good, the next call just mints again.
if tmp=$(mktemp "$DIR/.token-cache.XXXXXX"); then
  if jq -n --arg s "$shop" --arg c "$cid" --arg t "$token" --argjson e $((now + ${ttl:-86399})) \
       '{shop: $s, client_id: $c, access_token: $t, expires_at: $e}' >"$tmp" && chmod 600 "$tmp"; then
    mv -f "$tmp" "$CACHE" || rm -f "$tmp"
  else
    rm -f "$tmp"
  fi
fi

printf '%s' "$token"
