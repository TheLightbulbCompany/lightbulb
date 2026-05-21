#!/bin/sh
# Migrate one EFS user directory from the pre-normalization layout to the
# normalized layout, where every agent's workspace lives at workspaces/{id}/.
#
# Pre-migration (observed in prod):
#   workspaces/                  ← bare main-agent workspace (SOUL.md + runtime dirs)
#   workspaces/{custom_id}/      ← pre-existing upload dirs
#   agents/{id}/                 ← OpenClaw runtime (agent/, sessions/, qmd/)
#   openclaw.json                ← main inherits defaults.workspace; custom agents
#                                  have absolute-path `workspace` overrides like
#                                  "/home/node/agents/{id}"
#
# Post-migration:
#   workspaces/main/             ← main-agent workspace (everything from the old bare root)
#   workspaces/{custom_id}/      ← custom-agent workspace (uploads/ preserved, config files moved in)
#   agents/{id}/                 ← UNTOUCHED — OpenClaw runtime stays here
#   openclaw.json                ← agents.list has workspace="workspaces/{id}" for every agent
#
# Usage:
#   migrate-agent-workspace.sh /mnt/efs/users/<user_id>            # dry run
#   migrate-agent-workspace.sh --apply /mnt/efs/users/<user_id>    # execute
#
# Run inside the backend ECS task:
#   aws ecs execute-command --cluster <cluster> --task <task_id> \
#     --container backend --interactive \
#     --command "/bin/sh /app/scripts/migrate-agent-workspace.sh /mnt/efs/users/<uid>"
#
# Chokidar (polling mode) picks up the openclaw.json change and OpenClaw
# hot-reloads — no container restart needed, no API call needed.

set -eu

APPLY=0
if [ "${1:-}" = "--apply" ]; then
  APPLY=1
  shift
fi

USER_DIR="${1:-}"
if [ -z "$USER_DIR" ]; then
  echo "usage: $0 [--apply] /mnt/efs/users/<user_id>" >&2
  exit 2
fi

if [ ! -d "$USER_DIR" ]; then
  echo "error: $USER_DIR is not a directory" >&2
  exit 2
fi

WS="$USER_DIR/workspaces"
MAIN="$WS/main"
AGENTS="$USER_DIR/agents"
OC_JSON="$USER_DIR/openclaw.json"

# The 7 OpenClaw-seeded config files. These are the only filenames we move
# out of agents/{id}/ into workspaces/{id}/. Anything else in agents/{id}/
# (qmd/, agent/, sessions/, etc.) is OpenClaw runtime state and stays put.
CONFIG_ALLOWLIST="SOUL.md MEMORY.md TOOLS.md IDENTITY.md USER.md HEARTBEAT.md AGENTS.md"

echo "== migrate-agent-workspace =="
echo "  user_dir: $USER_DIR"
echo "  apply:    $APPLY"
echo

# ---------------------------------------------------------------------------
# Discover custom agent IDs from openclaw.json
# ---------------------------------------------------------------------------
#
# We need this to distinguish (at workspaces/ root) between main-agent content
# and pre-existing workspaces/{custom_id}/ dirs — the latter must NOT be moved
# into workspaces/main/{id}/ during Step 1. Reading openclaw.json is the only
# reliable way to know which IDs are custom agents.

CUSTOM_IDS=""
if [ -f "$OC_JSON" ]; then
  CUSTOM_IDS="$(python3 - "$OC_JSON" <<'PYEOF'
import json
import sys

try:
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    for a in cfg.get("agents", {}).get("list", []):
        aid = a.get("id")
        if aid and aid != "main":
            print(aid)
except Exception as exc:
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(1)
PYEOF
  )"
fi

echo "  custom agents from openclaw.json: ${CUSTOM_IDS:-<none>}"
echo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

mkdir_p() {
  dir="$1"
  if [ -d "$dir" ]; then
    return 0
  fi
  if [ "$APPLY" -eq 1 ]; then
    mkdir -p "$dir"
    echo "  mkdir: $dir"
  else
    echo "  would mkdir: $dir"
  fi
}

move_item() {
  src="$1"
  dst="$2"
  if [ ! -e "$src" ] && [ ! -L "$src" ]; then
    return 0
  fi
  # Use -e OR -L so a broken symlink at the destination still trips the skip.
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    echo "  SKIP (exists): $dst"
    return 0
  fi
  if [ "$APPLY" -eq 1 ]; then
    mv "$src" "$dst"
    echo "  moved: $src -> $dst"
  else
    echo "  would move: $src -> $dst"
  fi
}

is_custom_agent_id() {
  candidate="$1"
  for aid in $CUSTOM_IDS; do
    if [ "$candidate" = "$aid" ]; then
      return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------------------
# Step 1: main agent — workspaces/* (excl custom-agent dirs) -> workspaces/main/
# ---------------------------------------------------------------------------
#
# The old layout put all of main's files + runtime dirs at workspaces/ root.
# We move everything into workspaces/main/ EXCEPT:
#   - workspaces/main/ itself (target)
#   - workspaces/{custom_id}/ dirs — pre-existing upload dirs from the
#     upload endpoint or from OpenClaw's default resolution for custom
#     agents; those are ALREADY at the target location for the new layout.

if [ ! -d "$WS" ]; then
  echo "  workspaces/ doesn't exist — nothing to migrate for main"
else
  echo "== Step 1: main agent — workspaces/* -> workspaces/main/ =="
  mkdir_p "$MAIN"

  find "$WS" -mindepth 1 -maxdepth 1 -print | while IFS= read -r entry; do
    name="$(basename "$entry")"
    if [ "$name" = "main" ]; then
      continue
    fi
    if is_custom_agent_id "$name"; then
      echo "  SKIP (custom agent workspace already in place): $entry"
      continue
    fi
    move_item "$entry" "$MAIN/$name"
  done

  echo
fi

# ---------------------------------------------------------------------------
# Step 2: custom agents — move non-runtime content from agents/{id}/
# to workspaces/{id}/
# ---------------------------------------------------------------------------
#
# In the old layout, Config tab edits wrote SOUL.md etc. to agents/{id}/
# (via the pre-PR-267 backend); any other user-written files under
# agents/{id}/ would also be stranded once the backend starts reading from
# workspaces/{id}/. We move EVERYTHING from agents/{id}/ into
# workspaces/{id}/ EXCEPT known OpenClaw runtime subdirs (agent/, sessions/,
# qmd/) — those stay put because OpenClaw continues owning them there.
#
# In practice most prod custom agents only have runtime subdirs on EFS (their
# workspace pointed to container-local paths pre-fix), so this step usually
# moves nothing. But it's defensive: if a user DID write a note or artifact
# into agents/{id}/ somehow, we rescue it rather than strand it.

AGENTS_RUNTIME_SUBDIRS="agent sessions qmd"

is_agents_runtime_name() {
  candidate="$1"
  for known in $AGENTS_RUNTIME_SUBDIRS; do
    if [ "$candidate" = "$known" ]; then
      return 0
    fi
  done
  return 1
}

if [ ! -d "$AGENTS" ]; then
  echo "  agents/ doesn't exist — no custom-agent migration needed"
else
  echo "== Step 2: custom agents — agents/{id}/* (excl runtime) -> workspaces/{id}/ =="

  find "$AGENTS" -mindepth 1 -maxdepth 1 -type d -print | while IFS= read -r agent_dir; do
    agent_id="$(basename "$agent_dir")"
    target_ws="$WS/$agent_id"
    moved_any=0

    find "$agent_dir" -mindepth 1 -maxdepth 1 -print | while IFS= read -r entry; do
      name="$(basename "$entry")"
      if is_agents_runtime_name "$name"; then
        continue
      fi
      if [ "$moved_any" -eq 0 ]; then
        mkdir_p "$target_ws"
        moved_any=1
      fi
      move_item "$entry" "$target_ws/$name"
    done
  done

  echo
fi

# ---------------------------------------------------------------------------
# Step 3: patch openclaw.json so every agent's workspace = workspaces/{id}
# ---------------------------------------------------------------------------
#
# - Main: add/set workspace = "workspaces/main" (create list entry if missing).
# - Each custom agent: replace any existing `workspace` override with
#   "workspaces/{id}". Leave `agentDir` alone (still points to EFS via
#   .openclaw/agents/{id}/agent — unchanged by normalization).
#
# Chokidar polling picks up the file change and OpenClaw hot-reloads. No
# container restart, no API call needed.

if [ ! -f "$OC_JSON" ]; then
  echo "== Step 3: openclaw.json patch: skipped ($OC_JSON does not exist) =="
else
  echo "== Step 3: openclaw.json patch =="
  if [ "$APPLY" -eq 1 ]; then
    python3 - "$OC_JSON" <<'PYEOF'
import json
import sys

path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)

agents_cfg = cfg.setdefault("agents", {})
agents_list = agents_cfg.setdefault("list", [])

changed = False

# NOTE ON PATH FORMAT: OpenClaw resolves per-agent workspace values via
# path.resolve() against the container cwd (/home/node), NOT against the
# OpenClaw data root. So the value MUST start with ".openclaw/" for the
# resolved path to land inside the EFS mount (/home/node/.openclaw/). A
# bare "workspaces/main" would resolve to /home/node/workspaces/main/,
# which is container-local and ephemeral.

# 1) main: ensure the entry exists with workspace = ".openclaw/workspaces/main"
main_workspace = ".openclaw/workspaces/main"
found_main = False
for agent in agents_list:
    if agent.get("id") == "main":
        found_main = True
        if agent.get("workspace") != main_workspace:
            agent["workspace"] = main_workspace
            changed = True
        break
if not found_main:
    agents_list.append({"id": "main", "default": True, "workspace": main_workspace})
    changed = True

# 2) custom agents: rewrite any existing workspace override to
#    .openclaw/workspaces/{id} so it also lands on EFS.
for agent in agents_list:
    aid = agent.get("id")
    if not aid or aid == "main":
        continue
    desired = f".openclaw/workspaces/{aid}"
    if agent.get("workspace") != desired:
        agent["workspace"] = desired
        changed = True

if changed:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"  patched: {path}")
else:
    print(f"  already patched: {path}")
PYEOF
  else
    echo "  would patch: $OC_JSON"
    echo "    - agents.list[id=main].workspace = workspaces/main (add if missing)"
    echo "    - for each custom agent: workspace = workspaces/{id}"
  fi
  echo
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

if [ "$APPLY" -eq 0 ]; then
  echo "dry run complete. Re-run with --apply to perform moves + config patch."
else
  echo "migration complete."
  echo
  if [ -d "$WS" ]; then
    echo "Remaining in $WS/ (should be main/ + any custom-agent subdirs):"
    ls -la "$WS/"
  fi
  if [ -d "$AGENTS" ]; then
    echo
    echo "Remaining in $AGENTS/ (runtime dirs preserved — agent/, sessions/, qmd/):"
    ls -la "$AGENTS/"
  fi
fi
