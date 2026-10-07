---
name: instagram
description: Publish posts and Reels, moderate comments, and answer DMs on the owner's connected Instagram professional account via the Instagram API.
---

# Instagram

The owner has connected their Instagram professional account. Credentials live at
`/home/node/.openclaw/skills/instagram/credentials.json`.

**Always read the token through `token.sh`, never straight out of the JSON.** The
token expires 60 days after it is issued and `token.sh` renews it when it is
inside the last 30 days, so calling it is what keeps the connection alive:

```bash
IG_TOKEN=$(/home/node/.openclaw/skills/instagram/token.sh) || exit 1
IG_USER=$(jq -r .user_id /home/node/.openclaw/skills/instagram/credentials.json)
```

Re-run `token.sh` at the start of each task that touches Instagram. It is cheap
(no network call outside the renewal window) and safe to call repeatedly.

All calls go to `https://graph.instagram.com/v23.0`. Sanity check:

```bash
curl -s "https://graph.instagram.com/v23.0/me?fields=user_id,username&access_token=$IG_TOKEN"
```

## Publishing — ALWAYS confirm first

**Publishing is immediate, public, and visible to the account's followers. Never
call `media_publish` without the owner's explicit confirmation of the exact media
and caption in this conversation.** Draft → show the owner → publish only on a
clear yes.

Media must be at a **publicly reachable URL** (the API fetches it; local file
paths and private links fail). Two-step flow — create a container, then publish:

```bash
# Photo
CONTAINER=$(curl -s -X POST "https://graph.instagram.com/v23.0/$IG_USER/media" \
  -d "image_url=<public-image-url>" \
  --data-urlencode "caption=<caption>" \
  -d "access_token=$IG_TOKEN" | jq -r .id)

# Publish
curl -s -X POST "https://graph.instagram.com/v23.0/$IG_USER/media_publish" \
  -d "creation_id=$CONTAINER" -d "access_token=$IG_TOKEN"
```

Reels: use `media_type=REELS` + `video_url=`. Video containers process
asynchronously — poll until ready before publishing:

```bash
curl -s "https://graph.instagram.com/v23.0/$CONTAINER?fields=status_code&access_token=$IG_TOKEN"
# wait for "FINISHED" (retry every ~5s, give up after ~5min and report the error)
```

Carousels: create a container per child with `is_carousel_item=true`, then one
`media_type=CAROUSEL` container with `children=<id1>,<id2>,…`, then publish that.

## Comments

```bash
# List comments on a media object
curl -s "https://graph.instagram.com/v23.0/<media-id>/comments?access_token=$IG_TOKEN"

# Reply to a comment
curl -s -X POST "https://graph.instagram.com/v23.0/<comment-id>/replies" \
  --data-urlencode "message=<reply text>" -d "access_token=$IG_TOKEN"
```

## Direct messages

Replying to DMs is allowed within 24h of the user's last message:

```bash
curl -s -X POST "https://graph.instagram.com/v23.0/me/messages" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $IG_TOKEN" \
  -d '{"recipient":{"id":"<igsid>"},"message":{"text":"<reply>"}}'
```

## Errors

On `190` / "Invalid OAuth access token" / any 401: the connection has expired or
been revoked. Tell the owner to reconnect Instagram in **Settings → Connections**;
do not retry. `token.sh` already renews the token whenever it is used, so a
persistent auth error means the grant itself is gone — either revoked, or the
integration sat unused past its 60-day expiry, after which Meta will not refresh
it and only a fresh connect works.

Publishing is rate-limited (per-account posting quota); on a rate-limit error,
report it and wait — do not loop.
