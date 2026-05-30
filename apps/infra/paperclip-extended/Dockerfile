# Extended Paperclip image — a TEMPORARY OVERLAY on the official upstream image.
#
# WHY THIS EXISTS:
#   The upstream `openclaw-gateway` adapter hard-codes gateway protocol v3 and
#   injects a `paperclip` field into agentParams. OpenClaw's gateway requires v4
#   and rejects unknown agentParams fields (strict schema). The result is that
#   Paperclip agents cannot connect to our (v4) per-user OpenClaw containers —
#   every connect is rejected at the handshake. This overlay applies the upstream
#   fix-in-waiting on top of a pinned official image so we stop depending on
#   upstream's release cadence. See README.md for the full story + how to REVERT
#   once upstream ships it.
#
# The base is pinned BY DIGEST in `paperclip-version.json#upstream` (upstream
# publishes only :latest + sha- tags, so a digest is the only reproducible pin).
# That field is the SINGLE SOURCE: the build workflow passes it as the
# UPSTREAM_IMAGE build-arg, so this FROM cannot drift from the digest the
# workflow tags and the CDK deploys against. There is deliberately NO default —
# a `docker build` that forgets the arg fails closed instead of silently using a
# stale hard-coded pin. Manual builds must pass it explicitly:
#   docker build --build-arg UPSTREAM_IMAGE="$(node -p "require('./paperclip-version.json').upstream")" apps/infra/paperclip-extended/
ARG UPSTREAM_IMAGE
FROM ${UPSTREAM_IMAGE}

# The adapter is loaded from TypeScript SOURCE via the tsx loader (the image has
# no compiled dist for it — its package `exports` point at ./src/server/*.ts), so
# editing the source file takes effect at runtime with no rebuild.
#
# Three content-matched edits (survive minor upstream line shifts):
#   1) execute.ts — negotiate gateway protocol v4 instead of the hard-coded v3
#   2) execute.ts — remove the `paperclip` agentParams assignment the v4 gateway
#      rejects (that context already travels in the wake text via buildWakeText)
#   3) test.ts  — bump the connection-test probe off the hard-coded v3 too (it's a
#      literal, not the shared const, so edit 1 doesn't cover it), so a "Test
#      connection" probe isn't rejected for a stale protocol. Its full success
#      still depends on the Phase-2 relay/pairing; this just keeps protocol consistent.
#
# The verification lines FAIL the build if any edit didn't apply — so a base-image
# re-pin can never silently ship a still-v3 adapter. ENTRYPOINT/CMD/WORKDIR/ENV are
# inherited from the base unchanged.
#
# Run as root to edit /app (base image leaves User unset/root); the files are
# chowned back to the runtime uid (the task def runs as 1000:1000).
USER root
RUN set -eu; \
    ADAPTER=/app/packages/adapters/openclaw-gateway/src/server/execute.ts; \
    PROBE=/app/packages/adapters/openclaw-gateway/src/server/test.ts; \
    sed -i 's/^const PROTOCOL_VERSION = 3;$/const PROTOCOL_VERSION = 4;/' "$ADAPTER"; \
    sed -i '/^[[:space:]]*agentParams\.paperclip = paperclipPayload;[[:space:]]*$/d' "$ADAPTER"; \
    sed -i 's/minProtocol: 3,/minProtocol: 4,/; s/maxProtocol: 3,/maxProtocol: 4,/' "$PROBE"; \
    grep -qx 'const PROTOCOL_VERSION = 4;' "$ADAPTER" \
      || { echo 'PATCH FAILED: PROTOCOL_VERSION is not 4 — upstream code moved; re-check the patch'; exit 1; }; \
    if grep -q 'agentParams\.paperclip = paperclipPayload;' "$ADAPTER"; then \
      echo 'PATCH FAILED: the paperclip agentParams assignment is still present'; exit 1; \
    fi; \
    if grep -qE '(min|max)Protocol: 3,' "$PROBE"; then \
      echo 'PATCH FAILED: test.ts connection probe still advertises protocol 3'; exit 1; \
    fi; \
    chown 1000:1000 "$ADAPTER" "$PROBE"; \
    echo 'OK: openclaw-gateway adapter + test probe patched -> protocol v4 + paperclip field removed'
