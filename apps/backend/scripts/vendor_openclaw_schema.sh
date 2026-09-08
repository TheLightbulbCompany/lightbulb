#!/usr/bin/env bash
# Vendor the upstream OpenClaw config zod schema into the chaos suite as a
# self-contained Node bundle, so CI needs only `node` (no fork checkout, no
# GHCR auth) to validate emitted openclaw.json against the REAL schema.
# The fork checkout must be the clean commit named by the pinned fork-image tag;
# successful vendoring records both that image tag and the verified commit.
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

case "$PINNED_TAG" in
  ghcr.io/thelightbulbcompany/agent:*@* | ghcr.io/thelightbulbcompany/agent: )
    echo "ERROR: upstream must name a tagged Lightbulb fork image" >&2
    exit 1
    ;;
  ghcr.io/thelightbulbcompany/agent:* ) IMAGE_TAG="${PINNED_TAG#ghcr.io/thelightbulbcompany/agent:}" ;;
  * )
    echo "ERROR: upstream must name a tagged Lightbulb fork image" >&2
    exit 1
    ;;
esac
if [[ ! "$IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "ERROR: upstream must name a tagged Lightbulb fork image" >&2
  exit 1
fi
FORK_TAG="v$IMAGE_TAG"
if ! git check-ref-format "refs/tags/$FORK_TAG" >/dev/null 2>&1; then
  echo "ERROR: upstream must name a tagged Lightbulb fork image" >&2
  exit 1
fi
if ! SOURCE_COMMIT=$(git -C "$FORK" rev-parse --verify "refs/tags/${FORK_TAG}^{commit}" 2>/dev/null); then
  echo "ERROR: expected fork tag $FORK_TAG is missing; fetch and check out that release" >&2
  exit 1
fi
if ! HEAD_COMMIT=$(git -C "$FORK" rev-parse --verify HEAD 2>/dev/null) || [ "$HEAD_COMMIT" != "$SOURCE_COMMIT" ]; then
  echo "ERROR: fork HEAD does not match $FORK_TAG; refusing to mislabel the schema" >&2
  exit 1
fi
if ! git -C "$FORK" diff --quiet --ignore-submodules -- || ! git -C "$FORK" diff --cached --quiet --ignore-submodules --; then
  echo "ERROR: fork has tracked modifications; use the clean pinned release" >&2
  exit 1
fi
if ! UNTRACKED_INPUTS=$(git -C "$FORK" ls-files --others --exclude-standard -- src packages); then
  echo "ERROR: could not inspect fork source inputs" >&2
  exit 1
fi
if [ -n "$UNTRACKED_INPUTS" ]; then
  echo "ERROR: fork has untracked source inputs; use the clean pinned release" >&2
  exit 1
fi
echo "==> Verified fork source $FORK_TAG at $SOURCE_COMMIT"

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

# 4. Record the tag and verified commit this bundle was vendored from.
printf '%s\n' "$PINNED_TAG" > "$OUT/PINNED_TAG"
printf '%s\n' "$SOURCE_COMMIT" > "$OUT/PINNED_COMMIT"
echo "==> Vendored schema for $PINNED_TAG -> $OUT/openclaw-config-schema.mjs"
echo "==> Derived JSON Schema -> $JSON_OUT"
