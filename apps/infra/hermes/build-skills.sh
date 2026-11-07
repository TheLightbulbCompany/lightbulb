#!/usr/bin/env bash
# Build the Hermes image's bundled skills tree from `skills-src`.
#
# `skills-src/` is the source of truth for the agent-facing `lightbulb` skill. It moved here from apps/infra/openclaw/skills when the OpenClaw extended
# image was deleted; this image is now its only consumer. There is deliberately no overlay:
# per-file overlays shadowed newer source edits and shipped stale text, so wording a token
# swap cannot express is written in skills-src directly.
#
# The generated tree is NOT checked in -- a second copy is exactly the duplicate this
# replaced, and it had already drifted before the derivation existed.
#
# Run by .github/workflows/build-hermes-image.yml before `docker build` (the Dockerfile
# COPYs the generated `skills/`, which is gitignored) and by
# apps/backend/tests/unit/services/test_hermes_skill_tool_names.py, which asserts on the
# RESULT -- so the swap list below is the single place it is written down.
#
# Usage: build-skills.sh [DEST]   (default: apps/infra/hermes/skills, which is gitignored)
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
dest=${1:-$here/skills}

rm -rf "$dest"
mkdir -p "$dest"
cp -R "$here/skills-src/." "$dest/"

# MAINTENANCE.md is engineer-facing (it documents THIS build, among other things) and
# nothing in the image reads it -- Hermes discovers skills by SKILL.md front matter.
find "$dest" -name MAINTENANCE.md -delete

# The mechanical swaps. `exec` is Hermes's `terminal` toolset; the bundled skills live at a
# different path; the owner facts file is under $HERMES_HOME, not ~/.openclaw.
find "$dest" -type f -exec sed -i.bak \
  -e 's#/app/skills/#/opt/hermes-skills/#g' \
  -e 's#~/\.openclaw/lightbulb-facts\.json#$HERMES_HOME/lightbulb-facts.json#g' \
  -e 's#skill_workshop#skills#g' \
  -e 's#exec approval#terminal approval#g' \
  -e 's#exec-approval#terminal-approval#g' \
  -e 's#through `exec`#through `terminal`#g' \
  {} +
find "$dest" -name '*.bak' -delete

echo "built $(find "$dest" -type f | wc -l | tr -d ' ') skill files into $dest"
