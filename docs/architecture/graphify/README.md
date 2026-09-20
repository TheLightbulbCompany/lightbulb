# Graphify architecture export

`code-graph.json.gz` is a compact, GitHub-friendly Graphify export generated
2026-09-20 from the refreshed local graph at commit
`0bc33fb6a1569e63f781f69dc14fe2655e73edab`. It contains 22,207 AST-extracted
code nodes from 2,394 tracked files under `apps/`, `packages/`, and `.github/`,
plus 59,667 AST links whose endpoints are both retained.

The raw `graphify-out/` cache and semantic/doc nodes are deliberately excluded.
The source graph was refreshed without LLM calls. TypeScript/TSX coverage is
limited by Graphify's parser; rerun the exporter after refreshing the source
graph to publish a new snapshot:

```sh
python3 scripts/export-architecture-graph.py graphify-out/graph.json \
  docs/architecture/graphify/code-graph.json.gz
```

See the [current system architecture](../current-system.md) for the companion
human-readable overview. Run the regression check with:

```sh
python3 scripts/test-export-architecture-graph.py
```
