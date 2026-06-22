/*
 * Type declarations for the vendored lobster graph emitter.
 *
 * `lobsterGraph.js` is copied verbatim from @clawdbot/lobster@2026.5.22
 * (dist/src/workflows/graph.js) — the exact engine behind `lobster graph`
 * that the OpenClaw container runs. We feed it the raw parsed `.lobster`
 * YAML so the in-app graph matches the CLI output instead of a
 * reimplementation that drifts. The module is pure (no imports, no Node
 * deps). Re-vendor (and bump the version above) when the pinned
 * openclaw/lobster version changes; see vendor/README.md.
 */

export type LobsterGraphFormat = "mermaid" | "dot" | "ascii";

/** A workflow step as it appears in the parsed `.lobster` YAML. */
export interface LobsterWorkflowStep {
  id: string;
  [key: string]: unknown;
}

/** A parsed `.lobster` workflow document. */
export interface LobsterWorkflowDoc {
  steps: LobsterWorkflowStep[];
  [key: string]: unknown;
}

export function renderWorkflowGraph(input: {
  workflow: LobsterWorkflowDoc;
  format?: LobsterGraphFormat;
  args?: Record<string, unknown>;
}): string;
