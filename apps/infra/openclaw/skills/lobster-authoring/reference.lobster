name: reference-intake
# A complete, runnable lobster workflow using ONLY mechanisms proven on the embedded
# runner: durable state (state.get/set), explicit JSON threading, an explicit count
# (never .length), dot-index conditions, a for_each loop, and a real approval gate.
# No openclaw.invoke, no fast-io. Validate with: lobster run --dry-run reference.lobster
args:
  source:
    default: "demo"
env:
  # Point state at the per-agent EFS workspace so it survives container restarts.
  # (The platform sets LOBSTER_STATE_DIR globally; shown here for clarity.)
  LOBSTER_STATE_DIR: "/home/node/.openclaw/lobster-state"
steps:
  # 1. Read durable state: the set of ids already processed. -> array | null
  - id: seen
    pipeline: state.get reference-seen

  # 2. Produce items with a REAL program (node) -> command:, NOT pipeline:.
  - id: fetch
    command: >-
      node -e 'process.stdout.write(JSON.stringify([{"id":1,"kind":"a"},{"id":2,"kind":"b"}]))'

  # 3. Filter out already-seen ids. Prior output arrives via env (safe) + explicit stdin.
  #    Emit an explicit `count` field — never gate on `.length`.
  - id: fresh
    command: >-
      node -e 'const seen=JSON.parse(process.env.SEEN||"[]")||[];
        const all=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const items=all.filter(x=>!seen.includes(x.id));
        process.stdout.write(JSON.stringify({items:items,count:items.length}))'
    env:
      SEEN: "$seen.json"
    stdin: $fetch.stdout

  # 4. Process each fresh item. Gate the loop on the explicit count (dot-path, numeric).
  - id: process
    condition: $fresh.json.count > 0
    for_each: $fresh.json.items
    item_var: it
    steps:
      - id: handle
        command: >-
          node -e 'const x=JSON.parse(require("fs").readFileSync(0,"utf8"));
            process.stdout.write(JSON.stringify({handled:x.id}))'
        stdin: $it.json

  # 5. Human approval BEFORE any side effect.
  - id: gate
    approval:
      prompt: "Commit ${source} results?"

  # 6. The side effect, gated on approval.
  - id: commit
    command: node -e 'process.stdout.write("committed")'
    condition: $gate.approved

  # 7. Persist the new seen-set (old union fetched ids) for the next run.
  - id: newseen
    command: >-
      node -e 'const seen=JSON.parse(process.env.SEEN||"[]")||[];
        const all=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const ids=[...new Set([...seen,...all.map(x=>x.id)])];
        process.stdout.write(JSON.stringify(ids))'
    env:
      SEEN: "$seen.json"
    stdin: $fetch.stdout
  - id: remember
    pipeline: state.set reference-seen
    stdin: $newseen.json
