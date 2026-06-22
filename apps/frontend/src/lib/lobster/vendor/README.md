# Vendored lobster graph emitter

`lobsterGraph.js` is copied **verbatim** from
[`@clawdbot/lobster`](https://www.npmjs.com/package/@clawdbot/lobster)
`@2026.5.22`, file `dist/src/workflows/graph.js` — the exact code behind the
`lobster graph` CLI, and the version our OpenClaw container runs (the image
bakes `@openclaw/lobster@2026.6.6`, which depends on `@clawdbot/lobster@2026.5.22`).

We render the graph in the browser by parsing the `.lobster` YAML and passing
the raw document to `renderWorkflowGraph(...)`. Using lobster's own emitter
means the in-app graph matches `lobster graph` output exactly and tracks
upstream behavior (step types, `next`/`stdin`/`for_each`/`when` edges, mermaid
id-collision handling) instead of a reimplementation that silently drifts.

The module is pure: no imports, no Node APIs. `lobsterGraph.d.ts` provides the
types; `tsconfig` has `allowJs: true` with `checkJs` unset, so the vendored
`.js` is bundled but not type-checked.

## Re-vendoring (on an openclaw/lobster version bump)

```bash
cd /tmp && npm pack @clawdbot/lobster@<new-version>
tar xzf clawdbot-lobster-<new-version>.tgz
cp package/dist/src/workflows/graph.js \
   <repo>/apps/frontend/src/lib/lobster/vendor/lobsterGraph.js
# diff against the previous version; if renderWorkflowGraph's input shape or
# the format strings changed, update lobsterGraph.d.ts + index.ts to match.
```

Do not hand-edit `lobsterGraph.js` — keep it a clean copy so future diffs are trivial.
