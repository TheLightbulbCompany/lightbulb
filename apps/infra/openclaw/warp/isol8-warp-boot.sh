#!/bin/sh
# Cloudflare Zero Trust WARP — browser egress only.
#
# Purpose: give the container's Chrome a Cloudflare egress IP instead of the
# AWS NAT IP, which is what trips the datacenter-IP CAPTCHA walls. NOTHING
# ELSE in the container is routed through it: warp runs in "Local proxy" mode,
# which opens a SOCKS5/HTTP listener on loopback and affects only processes
# explicitly pointed at it. The gateway WebSocket, Paperclip, the bedrock-gate,
# Litestream/S3, EFS and every MCP server keep using the task's normal
# networking. The one pointer is Chrome's --proxy-pac-url, emitted by the
# backend config generator (core/containers/config.py) when
# BROWSER_EGRESS_PROXY_PORT is set.
#
# Why local-proxy mode and not a real tunnel: Fargate grants no NET_ADMIN and
# has no /dev/net/tun, so WARP cannot create its WireGuard/MASQUE interface.
# Proxy mode needs neither (verified against the official cloudflare-warp
# Debian package: warp-svc starts without dbus, without CAP_NET_ADMIN and
# without a tun device, and carries traffic over MASQUE/QUIC to 162.159.198.x).
#
# FAIL-OPEN BY CONSTRUCTION. Every exit here is 0. An unset/placeholder secret,
# a missing binary, a failed enrollment or a daemon that never reaches
# Connected all end with a log line, and the agent container boots exactly as
# it does today. Nothing on the boot path waits on this script.

set -u

log() { echo "[warp] $*"; }

ENROLL="${CLOUDFLARE_WARP_ENROLLMENT:-}"
if [ -z "$ENROLL" ]; then
  log "no CLOUDFLARE_WARP_ENROLLMENT; browser egress stays on the NAT IP"
  exit 0
fi

# The secret is created by CDK with a random placeholder and filled in by hand
# after deploy (same lifecycle as every other AuthStack secret), so "not valid
# enrollment JSON yet" is the NORMAL state until someone sets it — a skip, not
# an error.
ORG=$(printf '%s' "$ENROLL" | jq -er '.organization // empty' 2>/dev/null) || ORG=""
CLIENT_ID=$(printf '%s' "$ENROLL" | jq -er '.auth_client_id // empty' 2>/dev/null) || CLIENT_ID=""
CLIENT_SECRET=$(printf '%s' "$ENROLL" | jq -er '.auth_client_secret // empty' 2>/dev/null) || CLIENT_SECRET=""
if [ -z "$ORG" ] || [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
  log "CLOUDFLARE_WARP_ENROLLMENT is not {organization,auth_client_id,auth_client_secret} JSON; skipping"
  exit 0
fi

# The values below are interpolated into XML verbatim through an UNQUOTED
# heredoc, which re-parses `$`, backticks and backslashes as shell syntax.
# Cloudflare's team name and service-token pair are plain [A-Za-z0-9._-], so
# this never fires in practice — but a fat-fingered value with an XML
# metacharacter would produce a malformed mdm.xml that warp-svc ignores
# SILENTLY (only symptom: "never reaches Connected"), and one with a shell
# metacharacter would be EVALUATED by this script. Name both instead.
if printf '%s%s%s' "$ORG" "$CLIENT_ID" "$CLIENT_SECRET" | grep -q '[<>&"$`\\]'; then
  log "enrollment values contain XML or shell metacharacters; refusing to write mdm.xml"
  exit 0
fi

if ! command -v warp-svc >/dev/null 2>&1; then
  log "warp-svc is not in this image; pin a tag built from a commit whose apps/infra/openclaw/Dockerfile installs cloudflare-warp"
  exit 0
fi

PORT="${BROWSER_EGRESS_PROXY_PORT:-40000}"

# ---- Registration state: persist per owner on EFS ----------------------------
# warp-svc keeps its device registration in /var/lib/cloudflare-warp. On the
# image's ephemeral overlay that means a NEW Zero Trust device registration on
# every task start, and ZT registrations never expire, nothing auto-cleans
# service-token (non_identity) devices, and each one consumes a finite virtual
# IP — a fleet of containers restarting daily would accumulate dead devices
# forever. Pointing the dir at the owner's EFS mount gives ONE stable device
# per customer with no sweeper.
#
# Safe because per-user ECS services deploy under SINGLE_WRITER_DEPLOYMENT_CONFIG
# (maximumPercent=100, see ecs_manager.py): a customer never has two tasks
# alive at once, so the registration store never has two writers — the
# precondition the EFS/SQLite two-task corruption class needs.
#
# Same relocation pattern as the npm/uv/mcporter links in container-stack.ts's
# startupCommand, with one difference: the thing being replaced is the empty
# skeleton the cloudflare-warp package creates at IMAGE BUILD time, on the
# ephemeral overlay — never user data — so it is removed rather than archived.
# `ln -sfn` onto an existing real directory NESTS the link inside it instead of
# replacing it, so the remove must happen first and must be checked.
#
# NOT under .openclaw/state or .openclaw/agents: those paths are nested
# task-local NVMe volumes, which would put us right back on ephemeral storage.
WARP_STATE_DIR=/var/lib/cloudflare-warp
WARP_STATE_EFS=/home/node/.openclaw/.cloudflare-warp
if mkdir -p "$WARP_STATE_EFS" 2>/dev/null; then
  if [ ! -L "$WARP_STATE_DIR" ]; then
    rm -rf "$WARP_STATE_DIR" 2>/dev/null || true
  fi
  if [ -e "$WARP_STATE_DIR" ] && [ ! -L "$WARP_STATE_DIR" ]; then
    log "could not clear $WARP_STATE_DIR; using ephemeral registration for this task"
  else
    ln -sfn "$WARP_STATE_EFS" "$WARP_STATE_DIR" 2>/dev/null \
      || log "could not link $WARP_STATE_DIR to EFS; using ephemeral registration for this task"
  fi
else
  log "EFS mount not writable; using ephemeral registration for this task"
  mkdir -p "$WARP_STATE_DIR" 2>/dev/null || true
fi

# ---- Headless enrollment -----------------------------------------------------
# The presence of /var/lib/cloudflare-warp/mdm.xml makes warp-svc self-register
# with the service token at startup — no interactive login, no browser. Keys
# per Cloudflare's MDM parameter list:
#   service_mode=proxy + proxy_port  -> the loopback SOCKS5/HTTP listener
#   auto_connect=1                   -> minutes; any value forces Connected
#   onboarding=false                 -> no privacy-policy screen (headless)
#   switch_locked=true               -> the client cannot be disconnected
#                                       in-container (requires auto_connect)
#   warp_tunnel_protocol=masque      -> pinned: Local proxy mode requires MASQUE
# Deliberately NOT set: unique_client_id (would fight the persisted
# registration above) and override_api_endpoint.
umask 077
cat > "$WARP_STATE_DIR/mdm.xml" <<XML || { log "cannot write mdm.xml"; exit 0; }
<dict>
  <key>organization</key>
  <string>${ORG}</string>
  <key>auth_client_id</key>
  <string>${CLIENT_ID}</string>
  <key>auth_client_secret</key>
  <string>${CLIENT_SECRET}</string>
  <key>service_mode</key>
  <string>proxy</string>
  <key>proxy_port</key>
  <integer>${PORT}</integer>
  <key>auto_connect</key>
  <integer>1</integer>
  <key>switch_locked</key>
  <true/>
  <key>onboarding</key>
  <false/>
  <key>warp_tunnel_protocol</key>
  <string>masque</string>
</dict>
XML

# warp-svc logs are verbose (per-connection QUIC tracing at DEBUG), so they go
# to a file rather than CloudWatch. The tail is printed below only when the
# daemon fails to connect — the one time anyone wants it.
SVC_LOG=/tmp/warp-svc.log
warp-svc >"$SVC_LOG" 2>&1 &

# Two waits, because they fail for different reasons and a merged one would
# report the wrong cause: first the daemon's IPC socket (warp-cli can talk to
# it at all), then the tunnel (it reached Cloudflare).
i=0
while [ "$i" -lt 20 ] && ! warp-cli --accept-tos status >/dev/null 2>&1; do
  sleep 1
  i=$((i + 1))
done
if ! warp-cli --accept-tos status >/dev/null 2>&1; then
  log "warp-svc never accepted IPC after ${i}s; browser egress stays on the NAT IP"
  tail -20 "$SVC_LOG" 2>/dev/null
  exit 0
fi

# mdm.xml already pins mode and port (managed settings win over CLI ones), so
# this only asks the daemon to bring the tunnel up. Tolerate failure: it also
# returns non-zero when already connected.
warp-cli --accept-tos connect >/dev/null 2>&1 || true

# `warp-cli status` prints one of:
#   Status update: Connected
#   Status update: Disconnected. Reason: ...
# Match on ": Connected" — ": Disconnected" does not contain it.
i=0
while [ "$i" -lt 40 ]; do
  if warp-cli --accept-tos status 2>/dev/null | grep -q ': Connected'; then
    log "connected after ${i}s; SOCKS5 proxy on 127.0.0.1:${PORT}"
    exit 0
  fi
  sleep 1
  i=$((i + 1))
done

log "not Connected after ${i}s; browser egress stays on the NAT IP"
warp-cli --accept-tos status 2>&1 | head -3
tail -20 "$SVC_LOG" 2>/dev/null
exit 0
