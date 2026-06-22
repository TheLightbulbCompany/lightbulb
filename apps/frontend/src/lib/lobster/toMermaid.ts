import type { LobsterWorkflow, LobsterStep } from "./parseLobster";

const REF_RE = /\$([a-zA-Z0-9_-]+)/g;

// Pull out $step references from a text field. $load-connected.json.x yields
// "load-connected" (the regex stops at the dot). Only refs that name a real
// step become edges; $arg / {{template}} references are ignored.
export function extractStepRefs(
  text: string | undefined,
  knownIds: Set<string>,
): string[] {
  if (!text) return [];
  const out = new Set<string>();
  let m: RegExpExecArray | null;
  REF_RE.lastIndex = 0;
  while ((m = REF_RE.exec(text)) !== null) {
    if (knownIds.has(m[1])) out.add(m[1]);
  }
  return [...out];
}

function nodeId(stepId: string): string {
  return "n_" + stepId.replace(/[^a-zA-Z0-9_]/g, "_");
}

function escapeLabel(text: string): string {
  return text
    .replace(/"/g, "'")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\|/g, "/")
    .replace(/\s+/g, " ")
    .trim();
}

function truncate(text: string, max = 48): string {
  const t = text.trim();
  return t.length > max ? t.slice(0, max - 1) + "…" : t;
}

// Best-effort one-line summary of what a step does, for the node label.
function summarizeCommand(command?: string): string {
  if (!command) return "";
  const tool = command.match(/--tool\s+([a-zA-Z0-9_.-]+)/)?.[1];
  const action = command.match(/--action\s+([a-zA-Z0-9_.-]+)/)?.[1];
  if (tool) return action ? `${tool} · ${action}` : tool;
  return command.trim().split(/\s+/)[0] ?? "";
}

function stepLabel(step: LobsterStep): string {
  const parts = [escapeLabel(step.id)];
  const summary = summarizeCommand(step.command);
  if (summary) parts.push(escapeLabel(summary));
  return parts.join("<br/>");
}

// Emit a Mermaid `flowchart TD`. Nodes are steps (diamonds for approval
// gates). Edges come from $step references: condition refs carry the
// condition as an edge label and take precedence over a plain data edge
// for the same dependency.
export function toMermaid(workflow: LobsterWorkflow): string {
  const ids = new Set(workflow.steps.map((s) => s.id));
  const lines: string[] = ["flowchart TD"];

  for (const step of workflow.steps) {
    const id = nodeId(step.id);
    const label = stepLabel(step);
    lines.push(step.isApproval ? `  ${id}{"${label}"}` : `  ${id}["${label}"]`);
  }

  for (const step of workflow.steps) {
    const target = nodeId(step.id);
    const condRefs = new Set(extractStepRefs(step.condition, ids));
    const dataRefs = new Set([
      ...extractStepRefs(step.command, ids),
      ...extractStepRefs(step.stdin, ids),
      ...extractStepRefs(step.env, ids),
    ]);
    for (const ref of condRefs) {
      if (ref === step.id) continue;
      const label = escapeLabel(truncate(step.condition ?? ""));
      lines.push(`  ${nodeId(ref)} -->|"${label}"| ${target}`);
    }
    for (const ref of dataRefs) {
      if (ref === step.id || condRefs.has(ref)) continue;
      lines.push(`  ${nodeId(ref)} --> ${target}`);
    }
  }

  // Give approval gates a distinct warm style so human-in-the-loop steps are
  // scannable at a glance (the diamond shape alone is subtle).
  const approvals = workflow.steps.filter((s) => s.isApproval).map((s) => nodeId(s.id));
  if (approvals.length > 0) {
    lines.push("  classDef approval fill:#fff7ed,stroke:#b45309,color:#7c2d12;");
    lines.push(`  class ${approvals.join(",")} approval;`);
  }

  return lines.join("\n");
}
