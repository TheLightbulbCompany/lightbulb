#!/usr/bin/env bash
# Verify the Clerk social-sign-in redirect URIs are registered AT THE PROVIDERS.
#
# Why this exists: Clerk's change_domain moves the FAPI host, so its OAuth callback
# becomes https://clerk.<domain>/v1/oauth_callback. Clerk updates its own record and
# its dashboard SHOWS the new URI -- which reads as "done". It is not. In production
# Clerk forces custom credentials, so the redirect URI is registered at Google Cloud
# Console / Apple Developer, which Clerk cannot touch. After a domain move those still
# hold the OLD host and every social sign-in fails, with nothing on our side logging it
# (the failure happens on the provider's page, after the redirect leaves us).
#
# Run this after ANY domain move, before calling the migration done.
#
#   scripts/verify-oauth-redirects.sh [domain]      # default: flickmeon.ai
#
# Exits non-zero if either provider rejects the callback.

set -uo pipefail

DOMAIN="${1:-flickmeon.ai}"
CALLBACK="https://clerk.${DOMAIN}/v1/oauth_callback"

# From the Clerk dashboard: Configure -> SSO connections -> {Google,Apple}.
GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-1011290189072-7hro9nd8aj1utkoqmb5hs1odmv89hrps.apps.googleusercontent.com}"
APPLE_SERVICES_ID="${APPLE_SERVICES_ID:-co.isol8.signin}"   # immutable Apple identifier; stays on the old string forever

rc=0
echo "callback under test: $CALLBACK"
echo

# --- Google -------------------------------------------------------------
# Unregistered -> 302 to /signin/oauth/error with a base64 authError blob that
# decodes to "redirect_uri_mismatch". Registered -> 302 into the sign-in flow.
redirect=$(curl -sS --connect-timeout 10 --max-time 30 \
  -o /dev/null -w '%{redirect_url}' -G \
  "https://accounts.google.com/o/oauth2/v2/auth" \
  --data-urlencode "client_id=${GOOGLE_CLIENT_ID}" \
  --data-urlencode "redirect_uri=${CALLBACK}" \
  --data-urlencode "response_type=code" \
  --data-urlencode "scope=openid email profile") || redirect=""

# Fail CLOSED. Google signals a bad redirect_uri two ways: its own error page,
# and -- when the client/callback pair is otherwise valid -- an OAuth error
# bounced back to the callback itself with `error=` in the query. The second
# lands on a non-empty URL that is not the error page, so a naive "did it
# redirect somewhere" check calls a broken config healthy.
if [[ "$redirect" == *"/signin/oauth/error"* || "$redirect" == *"error="* ]]; then
  echo "Google: NOT REGISTERED (redirect_uri_mismatch)"
  echo "  fix: console.cloud.google.com/apis/credentials -> client ${GOOGLE_CLIENT_ID%%.*}"
  echo "       add '${CALLBACK}' to Authorized redirect URIs"
  rc=1
elif [[ -z "$redirect" ]]; then
  echo "Google: INCONCLUSIVE (no redirect returned)"
  rc=1
else
  echo "Google: OK (proceeds to sign-in)"
fi

# --- Apple --------------------------------------------------------------
# THE TRAP: Apple returns HTTP 200 whether or not the return URL is registered.
# Only the BODY differs, so a status-code check reports a broken config as healthy.
# Unregistered -> a short page carrying "Invalid web redirect" / "invalid_request".
apple_status=$(curl -sS --connect-timeout 10 --max-time 30 \
  -o /tmp/.apple-probe.$$ -w '%{http_code}' -G \
  "https://appleid.apple.com/auth/authorize" \
  --data-urlencode "client_id=${APPLE_SERVICES_ID}" \
  --data-urlencode "redirect_uri=${CALLBACK}" \
  --data-urlencode "response_type=code" \
  --data-urlencode "response_mode=form_post" \
  --data-urlencode "scope=name email") || apple_status="000"
body=$(cat /tmp/.apple-probe.$$ 2>/dev/null || true)
rm -f /tmp/.apple-probe.$$

if [[ "$apple_status" != "200" ]]; then
  echo "Apple: INCONCLUSIVE (HTTP ${apple_status}, expected 200)"
  rc=1
elif grep -qi "Invalid web redirect\|invalid_request\|unauthorized_client\|invalid_client" <<<"$body"; then
  echo "Apple: NOT REGISTERED (invalid web redirect)"
  echo "  fix: developer.apple.com -> Identifiers -> Services IDs -> ${APPLE_SERVICES_ID} -> Configure"
  echo "       Domains: clerk.${DOMAIN}   Return URLs: ${CALLBACK}"
  rc=1
elif ! grep -qi "appleid.apple.com\|Sign in to Apple\|<title>" <<<"$body" || [[ ${#body} -lt 20000 ]]; then
  # Unknown shape stays non-zero rather than optimistic: the size check alone
  # is a heuristic, so a positive marker has to be present too.
  echo "Apple: INCONCLUSIVE (no sign-in marker, ${#body} bytes)"
  rc=1
else
  echo "Apple: OK (real sign-in page, ${#body} bytes)"
fi

echo
[[ $rc -eq 0 ]] && echo "both providers registered." || echo "social sign-in is BROKEN -- users fall back to email_code."
exit $rc
