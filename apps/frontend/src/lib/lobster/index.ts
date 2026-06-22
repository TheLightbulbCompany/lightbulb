import { parseLobster, type LobsterWorkflow, type LobsterStep } from "./parseLobster";
import { toMermaid } from "./toMermaid";

export type LobsterRenderResult =
  | { ok: true; mermaid: string }
  | { ok: false; reason: "parse" | "empty"; message: string };

// Single entry point for the UI: turn raw .lobster content into mermaid text,
// or an explanatory failure. Never throws — the caller switches on the result.
export function lobsterToMermaid(content: string): LobsterRenderResult {
  let workflow: LobsterWorkflow;
  try {
    workflow = parseLobster(content);
  } catch (err) {
    return { ok: false, reason: "parse", message: (err as Error).message };
  }
  if (workflow.steps.length === 0) {
    return { ok: false, reason: "empty", message: "No workflow steps found to graph." };
  }
  return { ok: true, mermaid: toMermaid(workflow) };
}

export { parseLobster, toMermaid };
export type { LobsterWorkflow, LobsterStep };
