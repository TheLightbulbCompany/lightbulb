#!/usr/bin/env bash
# Vendor the upstream OpenClaw config zod schema into the chaos suite as a
# self-contained Node bundle, so CI needs only `node` (no fork checkout, no
# GHCR auth) to validate emitted openclaw.json against the REAL schema.
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
EOF
( cd "$FORK" && npx --yes esbuild "$TMP_ENTRY" --bundle --format=esm --platform=node \
    --outfile="$OLDPWD/$OUT/openclaw-config-schema.mjs" )
rm -f "$TMP_ENTRY"

# 3. Record the tag this bundle was vendored from (staleness guard reads it).
printf '%s\n' "$PINNED_TAG" > "$OUT/PINNED_TAG"
echo "==> Vendored schema for $PINNED_TAG -> $OUT/openclaw-config-schema.mjs"
