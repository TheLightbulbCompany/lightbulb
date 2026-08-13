#!/bin/bash
# Print the Instagram access token, renewing it first when it is inside the
# refresh window.
#
# Instagram's refresh takes the TOKEN ALONE (no app secret — see Meta's
# refresh_access_token reference), so this runs entirely in the container. The
# token is renewed exactly when it is used, which is the only moment it matters:
# no scheduler, no sweep, no backend round-trip.
#
# Meta's preconditions: the token must be at least 24h old and NOT yet expired.
# The 30-day window satisfies the age minimum by construction for a 60-day
# token. A token past expiry cannot be refreshed at all — Meta drops it
# permanently — so an integration left idle >60 days needs a fresh connect.
#
# Every failure path is non-fatal: we fall through and print the token we
# already hold. It is still valid (renewal starts 30 days before expiry), so the
# caller keeps working and the next invocation retries.
set -uo pipefail

CRED="${IG_CREDENTIALS:-/home/node/.openclaw/skills/instagram/credentials.json}"
WINDOW_SECONDS=$((30 * 24 * 3600))

if [ ! -r "$CRED" ]; then
  echo "instagram: no credentials at $CRED — reconnect in Settings → Connections" >&2
  exit 1
fi

token=$(jq -r '.access_token // empty' "$CRED" 2>/dev/null)
if [ -z "$token" ]; then
  echo "instagram: credentials hold no access_token — reconnect in Settings → Connections" >&2
  exit 1
fi

# No/unparseable expiry: treat as inside the window and attempt a renewal. Worst
# case Meta declines and we return the token unchanged.
remaining=0
expiry=$(jq -r '.token_expiry // empty' "$CRED" 2>/dev/null)
if [ -n "$expiry" ] && exp_epoch=$(date -d "$expiry" +%s 2>/dev/null); then
  remaining=$((exp_epoch - $(date +%s)))
fi

renewed=0
if [ "$remaining" -lt "$WINDOW_SECONDS" ]; then
  response=$(curl -sS --max-time 15 -G "https://graph.instagram.com/refresh_access_token" \
    --data-urlencode "grant_type=ig_refresh_token" \
    --data-urlencode "access_token=$token" 2>/dev/null) || response=""
  fresh=$(printf '%s' "$response" | jq -r '.access_token // empty' 2>/dev/null)
  ttl=$(printf '%s' "$response" | jq -r '.expires_in // empty' 2>/dev/null)
  if [ -n "$fresh" ] && [ -n "$ttl" ]; then
    new_expiry=$(date -u -d "@$(($(date +%s) + ttl))" +%Y-%m-%dT%H:%M:%S+00:00)
    # Same-dir temp + mv, so a crash mid-write never leaves a torn credential
    # file. Other fields (user_id, username) are carried through by jq.
    tmp=$(mktemp "$(dirname "$CRED")/.credentials.XXXXXX") || tmp=""
    if [ -n "$tmp" ]; then
      # Compare-and-swap. A reconnect may have rewritten credentials.json while
      # we sat in that curl — possibly for a DIFFERENT Instagram account. Blindly
      # writing our result would pair the new account's user_id/username with the
      # OLD account's token. The backend serializes its own writes under a
      # per-owner lock, but it cannot reach into this container, so the check
      # belongs here. If the file changed underneath us, ours is the stale token:
      # discard it and hand back whatever is actually on disk now.
      current=$(jq -r '.access_token // empty' "$CRED" 2>/dev/null)
      if [ -n "$current" ] && [ "$current" != "$token" ]; then
        rm -f "$tmp"
        token="$current"
      elif jq --arg t "$fresh" --arg e "$new_expiry" --argjson s "$ttl" \
             '.access_token = $t | .token_expiry = $e | .expires_in = $s' "$CRED" >"$tmp" 2>/dev/null &&
           chmod 600 "$tmp" && mv -f "$tmp" "$CRED"; then
        token="$fresh"
        renewed=1
      else
        rm -f "$tmp"
      fi
    fi
  fi
fi

# An expired token that could not be renewed is unusable, and Meta will never
# take it back. Emitting it anyway would let the documented
# `IG_TOKEN=$(token.sh) || exit 1` guard pass, pushing the failure into a
# confusing 401 several API calls later instead of the actionable message here.
if [ "$remaining" -le 0 ] && [ "$renewed" -eq 0 ]; then
  echo "instagram: token has expired and can no longer be refreshed — reconnect in Settings → Connections" >&2
  exit 1
fi

printf '%s' "$token"
