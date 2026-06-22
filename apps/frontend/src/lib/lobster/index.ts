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
  const workflow: LobsterWorkflowDoc = { ...(doc as Record<string, unknown>), steps };
  return { ok: true, mermaid: renderWorkflowGraph({ workflow, format: "mermaid" }) };
}

export { renderWorkflowGraph };
export type { LobsterWorkflowDoc, LobsterWorkflowStep };
