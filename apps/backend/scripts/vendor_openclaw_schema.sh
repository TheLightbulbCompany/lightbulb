#!/usr/bin/env bash
# Vendor the upstream OpenClaw config zod schema into the chaos suite as a
# self-contained Node bundle, so CI needs only `node` (no fork checkout, no
# GHCR auth) to validate emitted openclaw.json against the REAL schema.
#
# The bundle re-exports zod itself alongside the schema (costs nothing — zod is
# already inlined), which lets gen_json_schema.mjs run z.toJSONSchema against it
# with no npm install. That JSON Schema is what the BACKEND validates merged
# configs against at write time (core/services/config_schema.py) — the backend
# image is Python-only, so it cannot run the zod bundle directly.
#
# WHY A BUILD IS REQUIRED: OpenClawSchema (src/config/zod-schema.ts) transitively
# imports ~30 `@openclaw/*` workspace packages (via infra/, secrets/, routing/).
# Their package.json `exports` point at ./dist/*.mjs, which only exist after the
# monorepo is installed + built. So we build the fork once, THEN esbuild-bundle
# the schema against the resolved workspace.
#
# Run from the isol8 repo root. Requires the fork checkout (default ~/Desktop/agent)
# and a working pnpm + node toolchain.
set -euo pipefail

FORK="${OPENCLAW_FORK:-$HOME/Desktop/agent}"
OUT="apps/backend/tests/chaos/_schema"
JSON_OUT="apps/backend/core/services/openclaw_config.schema.json"
ENTRY_EXPORT="OpenClawSchema"                # static root schema in zod-schema.ts
SCHEMA_SRC="$FORK/src/config/zod-schema.ts"

PINNED_TAG=$(python3 -c "import json;print(json.load(open('openclaw-version.json'))['upstream'])")

if [ ! -f "$SCHEMA_SRC" ]; then
  echo "ERROR: fork schema not found at $SCHEMA_SRC (set OPENCLAW_FORK)" >&2
  exit 1
fi

# 1. Build the fork monorepo so workspace `dist/` + node_modules resolve.
echo "==> Installing + building fork at $FORK (this is the slow part)…"
( cd "$FORK" && pnpm install --frozen-lockfile && pnpm build )

# 2. Bundle the static root schema. Run esbuild from the fork root so its
#    node_modules (zod + built @openclaw/* workspace pkgs) resolve naturally.
mkdir -p "$OUT"
TMP_ENTRY="$FORK/.chaos-schema-entry.mjs"
cat > "$TMP_ENTRY" <<EOF
export { ${ENTRY_EXPORT} as ConfigSchema } from "./src/config/zod-schema.ts";
export { z } from "zod";
EOF
# The banner is load-bearing: the 8.1 schema transitively pulls CJS deps
# (hosted-git-info -> lru-cache) that call require() at module scope. Bundled as
# ESM without a require shim they throw "Dynamic require of node:X is not
# supported" at import time, which crashes gen_json_schema.mjs AFTER the shell
# has already truncated $JSON_OUT -- leaving a 0-byte schema that
# config_schema.py treats as "unavailable", silently disabling the write gate.
( cd "$FORK" && npx --yes esbuild "$TMP_ENTRY" --bundle --format=esm --platform=node \
    --banner:js="import{createRequire as __vendorCreateRequire}from'node:module';const require=__vendorCreateRequire(import.meta.url);" \
    --outfile="$OLDPWD/$OUT/openclaw-config-schema.mjs" )
rm -f "$TMP_ENTRY"

# 3. Derive the JSON Schema the Python backend validates against at write time.
#    Must stay in lockstep with the bundle — tests/chaos/test_schema_staleness.py
#    regenerates and diffs, so a re-vendor without this step fails CI.
JSON_TMP="$(mktemp)"
node "$OUT/gen_json_schema.mjs" > "$JSON_TMP"
if [ ! -s "$JSON_TMP" ] || ! node -e 'JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"))' "$JSON_TMP"; then
  rm -f "$JSON_TMP"
  echo "ERROR: generated JSON Schema is empty or invalid; leaving $JSON_OUT unchanged" >&2
  exit 1
fi
mv "$JSON_TMP" "$JSON_OUT"

# 4. Record the tag this bundle was vendored from (staleness guard reads it).
printf '%s\n' "$PINNED_TAG" > "$OUT/PINNED_TAG"
echo "==> Vendored schema for $PINNED_TAG -> $OUT/openclaw-config-schema.mjs"
echo "==> Derived JSON Schema -> $JSON_OUT"
