# Lobster Workflow Viewer — Design Spec

**Date:** 2026-06-21
**Status:** Implemented — see UPDATE below
**Surface:** Frontend (Next.js) — workspace file viewer in `/chat`

> **UPDATE (2026-06-22): graph generation switched from a hand-rolled emitter
> to vendoring lobster's own code.** The original plan hand-rolled a YAML→mermaid
> emitter mirroring lobster's documented edge rules. In review that proved to
> drift from real `lobster graph` behavior (it missed `env`/`when`/`for_each`
> handling and the sequential `next` edges, and reimplemented id-collision
> handling). lobster's actual emitter (`@clawdbot/lobster` `dist/src/workflows/graph.js`)
> is **pure and self-contained (zero imports, no Node deps)**, so we now **vendor
> it verbatim** (`src/lib/lobster/vendor/lobsterGraph.js`, pinned to the version
> our container runs) and feed it the raw parsed YAML. The in-app graph now
> matches `lobster graph` exactly and tracks upstream. `parseLobster.ts` /
> `toMermaid.ts` were deleted; `index.ts` is a thin parse + `renderWorkflowGraph`
> wrapper. Re-vendor when the openclaw/lobster pin bumps (see `vendor/README.md`).

## Goal

Let Isol8 users **see their agents' `.lobster` workflows as a visual flowchart**,
not as a wall of YAML in a plain textarea. When a `.lobster` file is opened in the
workspace file browser, default to a rendered graph (boxes for steps, arrows for
dependencies, diamonds for approval gates), with the existing editable text view one
tab away.

A **Phase 2** stretch adds an in-chat "view workflow" button when an agent references
a `.lobster` path in its output.

## Why this matters

`.lobster` files are deterministic, multi-step workflow specs (the execution engine
behind seed agents like Atlas and every pipeline-built agent). A real prod agent file
(`agents/echo/weekly-digest.lobster`, `agents/ora/morning-briefing.lobster`, the
`agents/pitch-scout/*` set) runs 100–200 lines with embedded JSON, curl, and node
one-liners. In today's viewer (`FileContentViewer.tsx`, a monospace `<textarea>`)
they are effectively unreadable. The workflow structure — which steps feed which,
which branch on a condition, where approvals gate — is exactly what a graph makes legible.

## Grounding / findings (why this design)

`lobster graph` is real (https://github.com/openclaw/lobster#visualizing-workflows):

```
lobster graph --file path/to/workflow.lobster [--format mermaid|dot|ascii]
```

- `mermaid` (default) emits `flowchart TD` text — renders directly in the browser with `mermaid.js`.
- `dot` emits Graphviz DOT; `ascii` emits a terminal node/edge list.
- Edges derive from `stdin: $step.stdout` / `$step.json` data-flow refs, `when:`/`condition:`
  conditionals, and approval gates (diamond nodes).

**We cannot run that CLI in the user's container**, verified against the repo:

1. The extended image (`apps/infra/openclaw/Dockerfile`, Layer 12) bakes
   `@openclaw/lobster@2026.6.6` **as a plugin only** (`/app/dist/extensions/lobster`).
   Its `package.json` has **no `bin`** — there is no `lobster` executable on PATH.
2. There is **no general "exec a shell command in the container" backend path**. The
   gateway pool (`core/gateway/connection_pool.py`) speaks OpenClaw protocol RPC
   (chat / agents / sessions), not arbitrary shell. The only way to run a command is the
   agent's own `exec` tool via a full LLM chat turn with approval gates — unsuitable for
   "click a file → see its graph." A dedicated exec RPC would be a security-sensitive new surface.

**Therefore: derive the graph client-side and render mermaid.** No container dependency,
no backend change, works even when the agent is asleep, instant.

## Non-goals

- No container CLI invocation; no new backend exec surface.
- No editing *of the graph* (graph is read-only; editing stays in the Code tab as today).
- No `dot`/`ascii` output formats in the UI (mermaid render is the in-browser target).
  A "Copy diagram source" of the generated mermaid is the only export in v1.
- Not building a separate "Workflows" route — this lives in the existing file viewer.
- Phase 2 (chat button) is designed here but ships after Phase 1.

## Architecture

Everything runs on the file content already loaded by `useWorkspaceFiles` — no new
network calls. The graph generator has **two interchangeable implementations behind one
interface**, chosen at planning/implementation time:

1. **Preferred — reuse lobster's real generator.** Investigate whether
   `@clawdbot/lobster`'s graph function (the code behind `lobster graph`) is
   browser-importable (pure; no `fs` / `child_process`). If yes, call it for
   canonical-fidelity mermaid with zero reimplementation.
2. **Fallback — small hand-rolled emitter.** If it is not importable, ship a focused
   parser + mermaid emitter mirroring the documented edge rules below.

The UI consumes a single `lobsterToMermaid(content: string): string` (or a thrown
error) and does not care which implementation is behind it.

`mermaid.js` is **dynamically imported only when the Graph tab activates** to keep it
out of the main chat bundle.

## Components & files

| File | Purpose |
|------|---------|
| `apps/frontend/src/lib/lobster/parseLobster.ts` | YAML → typed `LobsterWorkflow` model (steps: `id`, `command`, `condition`/`when`, `stdin`, approval detection). Uses the `yaml` parser dep. *(Skipped if generator path #1 works.)* |
| `apps/frontend/src/lib/lobster/toMermaid.ts` | `LobsterWorkflow` → `flowchart TD` string. Edge/branch/approval rules below. *(Skipped if path #1 works.)* |
| `apps/frontend/src/lib/lobster/index.ts` | Exposes `lobsterToMermaid(content) → string` — the single interface; wraps path #1 or the parser+emitter fallback. |
| `apps/frontend/src/components/chat/LobsterGraphView.tsx` | Dynamic-imports mermaid, renders the SVG themed to the cream/green palette (`#06402B`), scrollable container + zoom in/out/reset, "Copy diagram source" button, error fallback. |
| `apps/frontend/src/components/chat/FileContentViewer.tsx` | Adds a **Graph \| Code** tab header for `file.name.endsWith('.lobster')`; defaults to Graph; all other file types unchanged. |

New deps: `mermaid`, and a YAML parser (`yaml`) if the fallback emitter is used.
Existing `react-syntax-highlighter` is already present and unaffected.

## Data flow

```
useWorkspaceFiles → FileInfo{ name, path, content } → FileContentViewer
   └─ if name.endsWith('.lobster'): render Graph|Code tabs
        ├─ Graph (default): content → lobsterToMermaid → LobsterGraphView → mermaid.render() → SVG
        └─ Code: existing editable <textarea> + Save (unchanged)
   └─ else: existing behavior unchanged
```

## Edge-derivation rules (fallback emitter)

Mirrors the documented `lobster graph` behavior. Only used if generator path #1 fails.

- **Node per step**, keyed by `id`. Label = `id` (+ a short summary of the command/tool if available).
- **Data-flow edges:** scan each step's `command`, `stdin`, `condition`/`when`, and any
  `--args-json` payload for `$<step-id>` references (e.g. `$load-connected.json.platform`).
  For each referenced step, add edge `referenced → current`.
- **Roots:** steps with no incoming reference render as entry nodes.
- **Conditions:** a step's `condition`/`when` expression renders as the **edge label**
  on its incoming dependency edge(s) (truncated if long).
- **Approval gates:** steps using `approve` render as **diamond** nodes.
- Ordering is by dependency, not file order; independent steps render as parallel branches.

## Error handling

- **Invalid / unparseable YAML** → Graph tab shows an inline banner
  ("Couldn't render this workflow as a graph") with a one-click jump to the Code tab.
  Code tab always works.
- **Valid YAML but no `steps`** (or the pipeline-string form rather than the file form) →
  "No workflow steps found to graph" message; Code remains available.
- **mermaid render throws** → caught; show the generated diagram source plus the error.
  Never a blank pane.

## Testing

Per repo conventions, write tests per task; run targeted single-file vitest only at the
end — **never the full `pnpm test`** (RAM-heavy; see `feedback_frontend_vitest_ram_heavy`).

- **Parser unit tests** against real fixtures copied from `agents/*.lobster`
  (`echo/weekly-digest`, `ora/morning-briefing` — both exercise conditions/branches).
- **`toMermaid` snapshot tests** — assert nodes, dependency edges, condition edge labels,
  approval diamonds.
- **Component test** (`LobsterGraphView` / `FileContentViewer`) with mermaid mocked —
  assert tab switching, `.lobster` defaults to Graph, and error fallback to Code.

If generator path #1 (`@clawdbot/lobster` import) is used, the parser/emitter unit tests
collapse into a thin contract test around `lobsterToMermaid`.

## Phase 2 — in-chat "view workflow" button (designed, ships after Phase 1)

When agent output references a `.lobster` path, surface a small "View workflow" button
that opens that file's Graph view. Reuse `lib/filePathDetection.ts` for detection and
`LobsterGraphView` for rendering — no new graph logic. Open question for Phase 2: whether
the graph opens in the existing file-viewer panel or a lightweight modal over chat.

## Open questions / planning-time investigations

1. **Is `@clawdbot/lobster`'s graph generator browser-importable?** Determines
   generator path #1 vs the fallback emitter. Investigate first in the plan.
2. **mermaid version + Next 16 SSR:** confirm dynamic `import('mermaid')` + client-only
   render works cleanly (component is already `"use client"`).
3. **Large-graph ergonomics:** confirm zoom/scroll is sufficient for the biggest
   `pitch-scout` workflows, or whether fit-to-width defaults need tuning.

## Dependencies / risk

- Adds `mermaid` (heavy, but dynamically imported) and possibly `yaml`.
- Fallback emitter risks drift from upstream lobster's edge rules over time — mitigated by
  preferring path #1 (the real generator) and by snapshot tests pinned to real fixtures.
- No backend, infra, or container-image changes. Frontend-only; deploys via Vercel.
