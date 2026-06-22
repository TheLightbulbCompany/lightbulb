import { parse as parseYaml } from "yaml";

export interface LobsterStep {
  id: string;
  command?: string;
  condition?: string;
  stdin?: string;
  isApproval: boolean;
}

export interface LobsterWorkflow {
  name?: string;
  steps: LobsterStep[];
}

function asString(v: unknown): string | undefined {
  if (typeof v === "string") return v;
  if (v != null && typeof v === "object") return JSON.stringify(v);
  return undefined;
}

// A .lobster file is YAML: an optional `name` plus a `steps` list. Each step
// has an `id` and a `command`; `condition`/`stdin` are optional, and an
// `approval` field marks a human-gated step. We keep `stdin` as a string even
// when it parses to an object so $step references inside survive for edge
// detection downstream.
export function parseLobster(content: string): LobsterWorkflow {
  const doc = parseYaml(content);
  if (doc == null || typeof doc !== "object") {
    throw new Error("Not a valid lobster workflow (expected a YAML mapping).");
  }
  const record = doc as Record<string, unknown>;
  const rawSteps = Array.isArray(record.steps) ? record.steps : [];
  const steps: LobsterStep[] = [];
  for (const raw of rawSteps) {
    if (raw == null || typeof raw !== "object") continue;
    const r = raw as Record<string, unknown>;
    if (typeof r.id !== "string") continue;
    steps.push({
      id: r.id,
      command: asString(r.command),
      condition: asString(r.condition),
      stdin: asString(r.stdin),
      isApproval: "approval" in r,
    });
  }
  return {
    name: typeof record.name === "string" ? record.name : undefined,
    steps,
  };
}
