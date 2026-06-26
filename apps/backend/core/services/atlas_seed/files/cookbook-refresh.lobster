name: cookbook-refresh

# Daily 04:00 cron: refreshes the OpenClaw doc cache from the llms.txt index +
# agent snapshots from ~/.openclaw/agents/.
#
# Gateway-free contract (see intake-and-research.lobster header): the doc-index
# diff + page fetches run inside atlas-cookbook-reader.js over https, and the
# agent-snapshot walk reads the local filesystem — both genuine, not gateway
# state. The only state input (the prior refresh-state, used to diff the index)
# rides in on the REFRESH_STATE env var. The fetched pages, agent snapshots, and
# refresh-state are persisted via native state.set. state.* keys here:
# atlas/cookbook-refresh-state, atlas/cookbook-docs, atlas/cookbook-agent-snapshots,
# atlas/audit-log.

steps:
  # Step 1 — Load the prior refresh-state (carries indexed_urls for the diff).
  - id: state-load
    pipeline: state.get atlas/cookbook-refresh-state

  # Step 2 — Diff the live doc index (fetched over https inside the tool) against
  #          the cached indexed_urls (REFRESH_STATE env). Identifies added/removed.
  - id: diff-cache
    command: node atlas-cookbook-reader.js --action=diff-index
    env:
      REFRESH_STATE: $state-load.stdout
    on_error: continue

  # Step 3 — Fetch new/changed pages (capped at 50/run; https inside the tool).
  - id: fetch-pages
    command: node atlas-cookbook-reader.js --action=fetch-pages --max-fetches=50
    stdin: $diff-cache.stdout
    condition: $diff-cache.json.changed_count > 0
    on_error: continue

  # Step 4 — Merge the fetched pages into the durable doc cache (map url -> page).
  - id: docs-load
    condition: $diff-cache.json.changed_count > 0
    pipeline: state.get atlas/cookbook-docs

  # NO on_error: continue — feeds the docs state.set, must halt rather than wipe.
  # Defaults both inputs, so a crashed fetch-pages just leaves the cache unchanged.
  - id: docs-merge
    condition: $diff-cache.json.changed_count > 0
    command: >
      node -e 'const docs=JSON.parse(process.env.DOCS||"null")||{};
        const fp=JSON.parse(process.env.FETCHED||"null")||{};
        for (const p of (fp.fetched||[])) {
          if (p && p.url && p.body!=null) docs[p.url]={fetched_at:p.fetched_at, body:p.body};
        }
        process.stdout.write(JSON.stringify(docs));'
    env:
      DOCS: $docs-load.stdout
      FETCHED: $fetch-pages.stdout

  - id: docs-save
    condition: $diff-cache.json.changed_count > 0
    pipeline: state.set atlas/cookbook-docs
    stdin: $docs-merge.stdout

  # Step 5 — Snapshot prior agent builds from ~/.openclaw/agents/ (fs walk).
  - id: snapshot-load
    pipeline: state.get atlas/cookbook-agent-snapshots

  - id: snapshot-agents
    command: node atlas-cookbook-reader.js --action=snapshot-agents
    on_error: continue

  # Persist guard — store the fresh snapshot; on a crashed snapshot-agents fall
  # back to the prior snapshot so the cache is never wiped. NO on_error: continue.
  - id: snapshot-merge
    command: >
      node -e 'const snap=JSON.parse(process.env.SNAP||"null");
        const prev=JSON.parse(process.env.PREV||"null")||{count:0,agents:[]};
        const out=(snap&&Array.isArray(snap.agents))?snap:prev;
        process.stdout.write(JSON.stringify(out));'
    env:
      SNAP: $snapshot-agents.stdout
      PREV: $snapshot-load.stdout

  - id: snapshot-save
    pipeline: state.set atlas/cookbook-agent-snapshots
    stdin: $snapshot-merge.stdout

  # Step 6 — Update refresh-state timestamps + indexed_urls (so the next run can
  #          diff). Falls back to the prior indexed_urls when the diff crashed, so
  #          a bad run never blinds the next diff. NO on_error: continue.
  - id: state-next
    command: >
      node -e 'const prev=JSON.parse(process.env.PREV||"null")||{};
        const diff=JSON.parse(process.env.DIFF||"null")||{};
        const snap=JSON.parse(process.env.SNAP||"null")||{};
        const indexed=Array.isArray(diff.indexed_urls)?diff.indexed_urls:(prev.indexed_urls||[]);
        process.stdout.write(JSON.stringify({last_refreshed:new Date().toISOString(),
          indexed_urls:indexed,
          pages_added:diff.added_count||0,
          pages_changed:diff.changed_count||0,
          agents_snapshotted:snap.count||0}));'
    env:
      PREV: $state-load.stdout
      DIFF: $diff-cache.stdout
      SNAP: $snapshot-merge.stdout

  - id: state-save
    pipeline: state.set atlas/cookbook-refresh-state
    stdin: $state-next.stdout

  # Audit log — always appended. Defaults all inputs. Timestamp computed in-node.
  - id: audit-load
    pipeline: state.get atlas/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const diff=JSON.parse(process.env.DIFF||"null")||{};
        const snap=JSON.parse(process.env.SNAP||"null")||{};
        log.push({action:"cookbook_refreshed",
          pages_added:diff.added_count||0,
          pages_changed:diff.changed_count||0,
          agents_snapshotted:snap.count||0,
          timestamp:new Date().toISOString()});
        if (log.length>500) log.splice(0,log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      DIFF: $diff-cache.stdout
      SNAP: $snapshot-merge.stdout

  - id: audit-save
    pipeline: state.set atlas/audit-log
    stdin: $audit-next.stdout
