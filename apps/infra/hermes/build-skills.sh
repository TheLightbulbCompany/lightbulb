#!/usr/bin/env bash
# Build the Hermes image's bundled skills tree from the ONE source tree.
#
# The bundled skills are `apps/infra/openclaw/skills` with a handful of tokens swapped for
# Hermes's spelling, plus a small overlay for the few files whose CONTENT genuinely differs
# (Hermes has no ACP coding harness, no `openclaw backup create`, no Shopify plugin routes).
# Before this script the whole 6.4k-line tree was a second checked-in copy, and it had already
# drifted: `references/follow-ups.md` and the Shopify guidance reached only the OpenClaw side.
#
# Run by .github/workflows/build-hermes-image.yml before `docker build` (the Dockerfile's build
# context is apps/infra/hermes, so it cannot reach the OpenClaw tree itself) and by
# apps/backend/tests/unit/services/test_hermes_skill_tool_names.py, which asserts on the RESULT
# -- so the swap list below is the single place it is written down.
#
# Usage: build-skills.sh [DEST]   (default: apps/infra/hermes/skills, which is gitignored)
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
dest=${1:-$here/skills}

rm -rf "$dest"
mkdir -p "$dest"
cp -R "$here/../openclaw/skills/." "$dest/"

# Two references the Hermes build does NOT ship, matching what it shipped before this script:
#  - shopify/ drives lightbulb-channel plugin routes that exist only in the OpenClaw image.
#  - lightbulb/references/follow-ups.md is routed from the OpenClaw SKILL.md only; the Hermes
#    SKILL.md (skills-overlay/) has no routing row for it, so shipping it is dead weight.
# Re-enabling either is now a line here plus a routing row, not a 97-line second copy.
rm -rf "$dest/shopify" "$dest/lightbulb/references/follow-ups.md"

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

# The files whose content differs for a reason a token swap cannot express.
cp -R "$here/skills-overlay/." "$dest/"

echo "built $(find "$dest" -type f | wc -l | tr -d ' ') skill files into $dest"
