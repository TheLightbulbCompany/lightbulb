#!/usr/bin/env bash
# Prod domain cutover — vendor endpoint updates.
#
# RUN THIS ONLY AFTER the prod deploy has completed and api.flickmeon.ai serves
# traffic. Running it before means Stripe/Metronome POST to a host that does not
# answer yet; running it never means they POST to a host that stopped answering.
#
# Stripe URL edits are IN PLACE: `POST /v1/webhook_endpoints/{id}` may edit `url`
# and does NOT regenerate the signing secret, so isol8/prod/stripe_*_webhook_secret
# all stay valid. Do NOT "add a new endpoint" instead — routers/billing.py verifies
# against a SINGLE STRIPE_WEBHOOK_SECRET and every event would 400.
#
# ⛔ BEFORE THE FLIP: installed DESKTOP apps lose their update channel.
#
# desktop-build.yml sets `.plugins.updater.endpoints = [$updateUrl]` -- a
# SINGLE-element array -- to https://isol8.co/api/desktop/update/... . The flip
# moves the Vercel prod alias to flickmeon.ai and the synthesized prod templates
# no longer contain an `api.isol8.co` custom domain, so CFN deletes it. An app
# already installed then has no reachable updater, and therefore can never
# receive the build that would point it at the new domain. Manual reinstall only.
#
# MOBILE is fine: app.json sets updates.url = https://u.expo.dev/<project>, which
# is Expo's CDN and not our domain, so an OTA reaches installed apps regardless.
#
# Pick one before flipping:
#   a) ship a desktop build whose updater endpoint is flickmeon.ai, let installed
#      apps take it, and only then flip; or
#   b) keep isol8.co serving /api/desktop/update/* through the transition (this
#      is a dual-domain window, which the brief explicitly rules out); or
#   c) accept it and tell desktop users to reinstall -- fine only if the installed
#      count is genuinely ~0.
#
# Usage:  ./prod-domain-cutover.sh --check      (read-only; safe any time)
#         ./prod-domain-cutover.sh --apply      (mutates Stripe)
set -euo pipefail

NEW_HOST="api.flickmeon.ai"
OLD_HOST="api.isol8.co"
MODE="${1:---check}"

SK=$(aws secretsmanager get-secret-value --profile isol8-admin \
      --secret-id isol8/prod/stripe_secret_key --query SecretString --output text | tr -d '\r\n')

echo "== Stripe webhook endpoints =="
curl -s "https://api.stripe.com/v1/webhook_endpoints?limit=50" -u "$SK:" \
| MODE="$MODE" OLD_HOST="$OLD_HOST" NEW_HOST="$NEW_HOST" python3 -c "
import json,sys,os
mode=os.environ['MODE']; old=os.environ['OLD_HOST']; new=os.environ['NEW_HOST']
d=json.load(sys.stdin)
for e in d.get('data',[]):
    u=e['url']
    if old in u:
        print(f\"  {'WOULD EDIT' if mode!='--apply' else 'EDIT'}  {e['id']}\")
        print(f\"      {u}\")
        print(f\"   -> {u.replace(old,new)}\")
        if '/hooks/t/' in u:
            print('      !! OWNER AGENT TRIGGER — the fire URL is also the signature audience')
            print('         (paperclip_trigger_proxy.py:422). RE-ARM the trigger from Teams;')
            print('         editing the URL alone leaves signature verification failing.')
    else:
        print(f\"  ok        {e['id']}  {u}\")
"

if [ "$MODE" = "--apply" ]; then
  echo
  echo "== applying Stripe URL edits =="
  for id in $(curl -s "https://api.stripe.com/v1/webhook_endpoints?limit=50" -u "$SK:" \
              | OLD_HOST="$OLD_HOST" python3 -c "
import json,sys,os
d=json.load(sys.stdin)
print(' '.join(e['id'] for e in d.get('data',[]) if os.environ['OLD_HOST'] in e['url'] and '/hooks/t/' not in e['url']))
"); do
    cur=$(curl -s "https://api.stripe.com/v1/webhook_endpoints/$id" -u "$SK:" | python3 -c "import json,sys;print(json.load(sys.stdin)['url'])")
    new_url="${cur/$OLD_HOST/$NEW_HOST}"
    curl -s "https://api.stripe.com/v1/webhook_endpoints/$id" -u "$SK:" \
         --data-urlencode "url=$new_url" >/dev/null
    echo "  edited $id -> $new_url"
  done
  echo "  NOTE: owner agent-trigger endpoints are deliberately skipped — re-arm them from Teams."
fi

cat <<'MANUAL'

== Still manual (no API, or needs a human login) ==
  GitHub      REPO SECRETS the prod frontend build reads. These are not vendor
    secrets   config -- they are baked into the bundle at build time, so the app
              talks to whatever they said when it was built:
                NEXT_PUBLIC_API_URL_PROD            -> https://api.flickmeon.ai/api/v1
                NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_PROD -> the NEW pk_live_
              Update BOTH, then re-run the prod deploy so a build picks them up.
              (Confirm with `gh secret list --repo TheLightbulbCompany/lightbulb`: the _DEV twins
               were updated on the dev flip, so their timestamps show the pattern.)
              Missing NEXT_PUBLIC_API_URL_PROD = the prod app calls a dead host.

  Metronome   webhook URL -> https://api.flickmeon.ai/api/v1/webhooks/metronome
              (api key at isol8/prod/metronome_api_key; drives the paid-chat
               fast-gate flag + the Bedrock kill-switch)
  Clerk       Change domain -> clerk.flickmeon.ai. Then: the DNS records Clerk
              renders (READ them off Dashboard -> Domains; the set is
              per-instance and is NOT documented anywhere -- do not pre-write
              from a guessed list), certificates, the NEW pk_live_ into BOTH
              the GitHub secret AND apps/mobile/{eas.json,package.json} where
              it is hardcoded, isol8/prod/clerk_issuer, then redeploy.
              Also: Google redirect URI, a NEW Apple Services ID + relay source.
              The window between the domain flip and the redeploy is a
              BROKEN-APP window -- Clerk fails to load on a stale key.
  Apple       App Store Server Notifications URL; AASA on the new domain
  Meta        2 apps: redirect URIs + webhook callback + verify token
  Termly      website URL (legal entity name waits for the amendment)
  PostHog     allowed/authorized URLs
  Clerk       ALSO rename the APPLICATION (dev + prod instances) -- the
              sign-in card reads "Continue to Isol8" and that string is
              dashboard config no code change can reach. Independent of
              the domain change; safe to do any time.
  Owners      re-arm inbound triggers; reconnect BYO-Google + DCR integrations
MANUAL
