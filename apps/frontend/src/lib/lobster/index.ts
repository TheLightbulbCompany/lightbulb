import { parse as parseYaml } from "yaml";
import {
  renderWorkflowGraph,
  type LobsterWorkflowDoc,
  type LobsterWorkflowStep,
} from "./vendor/lobsterGraph";

export type LobsterRenderResult =
  | { ok: true; mermaid: string }
  | { ok: false; reason: "parse" | "empty"; message: string };

function hasStringId(step: unknown): step is LobsterWorkflowStep {
  return (
    step != null &&
    typeof step === "object" &&
    typeof (step as { id?: unknown }).id === "string"
  );
}

// The vendored emitter (see vendor/lobsterGraph.js) embeds each step's
// command/run preview verbatim into the mermaid node label and escapes only
// quotes — not newlines. Folded (`>`) or literal (`|`) YAML scalars therefore
// leak raw newlines into `["..."]` node labels, which splits the node statement
// across lines and makes mermaid.render() throw. These are the step fields that
// become label text; collapsing their whitespace before graphing keeps each
// label on one line. The sanitized copy is display-only, so workflow execution
// is unaffected.
const LABEL_TEXT_FIELDS = [
  "command",
  "run",
  "pipeline",
  "workflow",
  "for_each",
  "when",
  "condition",
] as const;

function collapseWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function sanitizeStepForGraph(step: LobsterWorkflowStep): LobsterWorkflowStep {
  const next: LobsterWorkflowStep = { ...step };
  for (const field of LABEL_TEXT_FIELDS) {
    const value = next[field];
    if (typeof value === "string") next[field] = collapseWhitespace(value);
  }
  return next;
}

// Single entry point for the UI: parse the raw `.lobster` YAML and hand it to
// lobster's own graph emitter (see vendor/) so our output matches `lobster
// graph` exactly. Never throws — the caller switches on the result.
export function lobsterToMermaid(content: string): LobsterRenderResult {
  let doc: unknown;
  try {
    doc = parseYaml(content);
  } catch (err) {
    return {
      ok: false,
      reason: "parse",
      message: err instanceof Error ? err.message : String(err),
    };
  }
  if (doc == null || typeof doc !== "object" || Array.isArray(doc)) {
    return {
      ok: false,
      reason: "parse",
      message: "Not a valid lobster workflow (expected a YAML mapping).",
    };
  }
  const rawSteps = (doc as { steps?: unknown }).steps;
  // The emitter keys nodes/edges off each step's id; drop anything without a
  // string id (matches lobster's own schema assumption).
  const steps = Array.isArray(rawSteps) ? rawSteps.filter(hasStringId) : [];
  if (steps.length === 0) {
    return { ok: false, reason: "empty", message: "No workflow steps found to graph." };
  }
  const workflow: LobsterWorkflowDoc = {
    ...(doc as Record<string, unknown>),
    steps: steps.map(sanitizeStepForGraph),
  };
  // The emitter joins each label's id and command preview with a literal "\n";
  // flatten any remaining literal "\n" (the id separator plus escape sequences
  // inside command text) to a space so labels render on one readable line. Real
  // newlines were already collapsed in sanitizeStepForGraph above.
  const mermaid = renderWorkflowGraph({ workflow, format: "mermaid" }).replace(/\\n/g, " ");
  return { ok: true, mermaid };
}

export { renderWorkflowGraph };
export type { LobsterWorkflowDoc, LobsterWorkflowStep };
