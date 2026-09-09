#!/usr/bin/env bash
# Vendor OpenClaw's config schema as a self-contained ESM bundle and the JSON
# Schema consumed by the Python backend. With no arguments this preserves the
# legacy fleet artifact layout. An explicit candidate requires complete source
# provenance and publishes immutable versioned artifacts.
set -euo pipefail

ROOT=$(pwd)
LEGACY_OUT="apps/backend/tests/chaos/_schema"
LEGACY_JSON="apps/backend/core/services/openclaw_config.schema.json"
RELEASES_OUT="$LEGACY_OUT/releases"
SCHEMAS_OUT="apps/backend/core/services/openclaw_config_schemas"
ENTRY_EXPORT="OpenClawSchema"

usage() {
  echo "usage: $0 [--source PATH --tag vTAG --commit SHA --schema-id TAG]" >&2
  exit 2
}

SOURCE=""
FORK_TAG=""
EXPECTED_COMMIT=""
SCHEMA_ID=""
EXPLICIT=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source|--tag|--commit|--schema-id)
      [ "$#" -ge 2 ] || usage
      EXPLICIT=true
      case "$1" in
        --source) SOURCE=$2 ;;
        --tag) FORK_TAG=$2 ;;
        --commit) EXPECTED_COMMIT=$2 ;;
        --schema-id) SCHEMA_ID=$2 ;;
      esac
      shift 2
      ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

if $EXPLICIT; then
  [ -n "$SOURCE" ] && [ -n "$FORK_TAG" ] && [ -n "$EXPECTED_COMMIT" ] && [ -n "$SCHEMA_ID" ] || usage
  [[ "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "ERROR: --commit must be a full lowercase commit SHA" >&2; exit 1; }
  [[ "$SCHEMA_ID" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || { echo "ERROR: invalid schema id" >&2; exit 1; }
  [ "$FORK_TAG" = "v$SCHEMA_ID" ] || { echo "ERROR: schema id must exactly match the candidate tag without v" >&2; exit 1; }
  PINNED_TAG="$FORK_TAG"
else
  SOURCE="${OPENCLAW_FORK:-$HOME/Desktop/agent}"
  PINNED_IMAGE=$(python3 -c "import json;print(json.load(open('openclaw-version.json'))['upstream'])")
  case "$PINNED_IMAGE" in
    ghcr.io/thelightbulbcompany/agent:*@* | ghcr.io/thelightbulbcompany/agent:)
      echo "ERROR: upstream must name a tagged Lightbulb fork image" >&2
      exit 1
      ;;
    ghcr.io/thelightbulbcompany/agent:*) SCHEMA_ID="${PINNED_IMAGE#ghcr.io/thelightbulbcompany/agent:}" ;;
    *) echo "ERROR: upstream must name a tagged Lightbulb fork image" >&2; exit 1 ;;
  esac
  [[ "$SCHEMA_ID" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || { echo "ERROR: upstream must name a tagged Lightbulb fork image" >&2; exit 1; }
  FORK_TAG="v$SCHEMA_ID"
  PINNED_TAG="$PINNED_IMAGE"
fi

SCHEMA_SRC="$SOURCE/src/config/zod-schema.ts"
[ -f "$SCHEMA_SRC" ] || { echo "ERROR: fork schema not found at $SCHEMA_SRC" >&2; exit 1; }
git check-ref-format "refs/tags/$FORK_TAG" >/dev/null 2>&1 || { echo "ERROR: invalid source tag" >&2; exit 1; }

verify_source() {
  local tag_object source_commit head_commit untracked_inputs
  tag_object=$(git -C "$SOURCE" rev-parse --verify "refs/tags/$FORK_TAG" 2>/dev/null) || {
    echo "ERROR: expected fork tag $FORK_TAG is missing" >&2
    return 1
  }
  [ "$(git -C "$SOURCE" cat-file -t "$tag_object" 2>/dev/null)" = tag ] || {
    echo "ERROR: expected fork tag $FORK_TAG must be annotated" >&2
    return 1
  }
  source_commit=$(git -C "$SOURCE" rev-parse --verify "refs/tags/${FORK_TAG}^{commit}" 2>/dev/null) || return 1
  if [ -n "$EXPECTED_COMMIT" ] && [ "$source_commit" != "$EXPECTED_COMMIT" ]; then
    echo "ERROR: peeled tag commit does not match --commit" >&2
    return 1
  fi
  head_commit=$(git -C "$SOURCE" rev-parse --verify HEAD 2>/dev/null) || return 1
  [ "$head_commit" = "$source_commit" ] || {
    echo "ERROR: fork HEAD does not match $FORK_TAG" >&2
    return 1
  }
  if ! git -C "$SOURCE" diff --quiet --ignore-submodules -- || ! git -C "$SOURCE" diff --cached --quiet --ignore-submodules --; then
    echo "ERROR: fork has tracked modifications" >&2
    return 1
  fi
  untracked_inputs=$(git -C "$SOURCE" ls-files --others --exclude-standard) || {
    echo "ERROR: could not inspect fork source inputs" >&2
    return 1
  }
  [ -z "$untracked_inputs" ] || {
    echo "ERROR: fork has untracked source inputs" >&2
    return 1
  }
  SOURCE_COMMIT=$source_commit
}

verify_source
echo "==> Verified fork source $FORK_TAG at $SOURCE_COMMIT"

NODE_VERSION=$(node -p 'process.versions.node')
node -e '
const [major, minor] = process.versions.node.split(".").map(Number);
if (!((major === 24 && minor >= 16) || (major === 26 && minor >= 1) || major > 26)) process.exit(1);
' || { echo "ERROR: Node $NODE_VERSION is unsupported; need >=24.16.0 <25 or >=26.1.0" >&2; exit 1; }

PACKAGE_MANAGER=$(node -e 'const p=require(process.argv[1]).packageManager||"";process.stdout.write(p)' "$SOURCE/package.json")
[[ "$PACKAGE_MANAGER" =~ ^pnpm@([0-9]+\.[0-9]+\.[0-9]+)(\+sha512\.[0-9a-f]{128})?$ ]] || {
  echo "ERROR: fork packageManager must pin an exact pnpm release" >&2
  exit 1
}
PINNED_PNPM=${BASH_REMATCH[1]}
ACTUAL_PNPM=$(cd "$SOURCE" && pnpm --version)
[ "$ACTUAL_PNPM" = "$PINNED_PNPM" ] || {
  echo "ERROR: fork requires pnpm $PINNED_PNPM, found $ACTUAL_PNPM" >&2
  exit 1
}

echo "==> Installing + building fork at $SOURCE"
(cd "$SOURCE" && pnpm install --frozen-lockfile && pnpm build)
verify_source

ESBUILD="$SOURCE/node_modules/.bin/esbuild"
[ -x "$ESBUILD" ] || { echo "ERROR: pinned esbuild is not installed at $ESBUILD" >&2; exit 1; }

mkdir -p "$RELEASES_OUT" "$SCHEMAS_OUT"
TMP_ROOT=$(mktemp -d "$ROOT/.vendor-openclaw-schema.XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT
TMP_RELEASE="$TMP_ROOT/release"
TMP_JSON="$TMP_ROOT/schema.json"
mkdir "$TMP_RELEASE"
TMP_BUNDLE="$TMP_RELEASE/openclaw-config-schema.mjs"

printf 'export { %s as ConfigSchema } from "./src/config/zod-schema.ts";\nexport { z } from "zod";\n' "$ENTRY_EXPORT" |
  (cd "$SOURCE" && "$ESBUILD" --bundle --format=esm --platform=node --target=node20 \
    --sourcefile=vendor-openclaw-schema.ts --loader=ts \
    --banner:js="import{createRequire as __vendorCreateRequire}from'node:module';const require=__vendorCreateRequire(import.meta.url);" \
    --outfile="$TMP_BUNDLE")

if ! node "$ROOT/$LEGACY_OUT/gen_json_schema.mjs" "$TMP_BUNDLE" > "$TMP_JSON" || \
  [ ! -s "$TMP_JSON" ] || \
  ! node -e 'JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"))' "$TMP_JSON"; then
  echo "ERROR: generated JSON Schema is empty or invalid; published artifacts unchanged" >&2
  exit 1
fi

BUNDLE_SHA256=$(shasum -a 256 "$TMP_BUNDLE" | awk '{print $1}')
JSON_SHA256=$(shasum -a 256 "$TMP_JSON" | awk '{print $1}')
verify_source

if $EXPLICIT; then
  FINAL_RELEASE="$RELEASES_OUT/$SCHEMA_ID"
  FINAL_JSON="$SCHEMAS_OUT/$SCHEMA_ID.json"
  if [ -e "$FINAL_RELEASE" ] || [ -e "$FINAL_JSON" ]; then
    echo "ERROR: candidate schema $SCHEMA_ID already exists; refusing to replace immutable artifacts" >&2
    exit 1
  fi
  python3 - "$TMP_RELEASE/PROVENANCE.json" "$SCHEMA_ID" "$FORK_TAG" "$SOURCE_COMMIT" "$BUNDLE_SHA256" "$JSON_SHA256" <<'PY'
import json
import sys

path, schema_id, tag, commit, bundle_hash, json_hash = sys.argv[1:]
with open(path, "w") as stream:
    json.dump(
        {
            "schema_id": schema_id,
            "tag": tag,
            "commit": commit,
            "bundle_sha256": bundle_hash,
            "json_sha256": json_hash,
        },
        stream,
        indent=2,
        sort_keys=True,
    )
    stream.write("\n")
PY
  mv "$TMP_RELEASE" "$FINAL_RELEASE"
  mv "$TMP_JSON" "$FINAL_JSON"
  echo "==> Vendored candidate schema $SCHEMA_ID -> $FINAL_RELEASE"
  echo "==> Derived JSON Schema -> $FINAL_JSON"
else
  TMP_TAG="$TMP_ROOT/PINNED_TAG"
  TMP_COMMIT="$TMP_ROOT/PINNED_COMMIT"
  printf '%s\n' "$PINNED_TAG" > "$TMP_TAG"
  printf '%s\n' "$SOURCE_COMMIT" > "$TMP_COMMIT"
  mv "$TMP_BUNDLE" "$LEGACY_OUT/openclaw-config-schema.mjs"
  mv "$TMP_JSON" "$LEGACY_JSON"
  mv "$TMP_TAG" "$LEGACY_OUT/PINNED_TAG"
  mv "$TMP_COMMIT" "$LEGACY_OUT/PINNED_COMMIT"
  echo "==> Vendored fleet schema for $PINNED_TAG -> $LEGACY_OUT/openclaw-config-schema.mjs"
  echo "==> Derived JSON Schema -> $LEGACY_JSON"
fi
