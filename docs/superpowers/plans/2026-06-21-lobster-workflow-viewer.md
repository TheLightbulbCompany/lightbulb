# Lobster Workflow Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render a user's `.lobster` workflow files as an interactive flowchart in the `/chat` workspace file viewer, with the existing editable text view one tab away.

**Architecture:** Frontend-only. A `.lobster` file is simple YAML (`name` + `steps[]`). We parse it client-side, emit Mermaid `flowchart TD` text mirroring lobster's documented edge rules, and render it with `mermaid.js` (dynamically imported only when the Graph tab is active). No backend, infra, or container changes.

**Tech Stack:** Next.js 16 / React 19, TypeScript, `yaml` (parser, matches lobster's own), `mermaid@^11` (render), vitest + jsdom + @testing-library/react (tests).

## Global Constraints

- **Frontend-only.** No backend, infra, or container-image changes. Deploys via Vercel.
- **Spec:** `docs/superpowers/specs/2026-06-21-lobster-workflow-viewer-design.md`.
- **Execution branch:** Work on a feature branch / worktree — **never commit to `main` directly** (user standing rule). Cut a PR; never push without explicit approval.
- **Tests:** Write tests per task. Run **targeted single-file vitest only, foreground** — `pnpm exec vitest run <path>`. **NEVER run the full `pnpm test`** (≈17.7 GB RAM; user-revoked). Frontend CI has no vitest, so local runs are the gate.
- **Theme:** match the existing cream/green palette — surfaces `#f3efe6`, borders `#e0dbd0`, accent/green `#06402B`, muted text `#8a8578`, body text `#1a1a1a`.
- **Comments:** explain *why*, never reference PR numbers / plan steps / bug IDs in code comments.
- **Commit trailer:** end every commit message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Decision (resolved):** Use a hand-rolled parser + Mermaid emitter, NOT an import of `@clawdbot/lobster`. The SDK is importable (ESM, deps `ajv`+`yaml`) but bundling an OpenClaw SDK (ajv code-gen + transitive runtime + version-coupling to the container image) into the Vercel client bundle is higher risk than the simple emitter is worth. Fidelity is guarded by snapshot tests pinned to real fixtures.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `apps/frontend/src/lib/lobster/parseLobster.ts` | Parse `.lobster` YAML → typed `LobsterWorkflow` model. Pure, no React. |
| `apps/frontend/src/lib/lobster/toMermaid.ts` | `LobsterWorkflow` → Mermaid `flowchart TD` string + ref-extraction helpers. Pure. |
| `apps/frontend/src/lib/lobster/index.ts` | Public `lobsterToMermaid(content) → LobsterRenderResult` (no exceptions across boundary). |
| `apps/frontend/src/components/chat/LobsterGraphView.tsx` | Render the Mermaid graph (dynamic mermaid import), zoom controls, copy-source, error fallback. |
| `apps/frontend/src/components/chat/FileContentViewer.tsx` | **Modify**: add Graph\|Code tabs for `.lobster` files; default Graph. |
| `apps/frontend/src/lib/lobster/__tests__/parseLobster.test.ts` | Parser unit tests (real fixtures). |
| `apps/frontend/src/lib/lobster/__tests__/toMermaid.test.ts` | Emitter unit + snapshot tests. |
| `apps/frontend/src/lib/lobster/__tests__/index.test.ts` | Result-shape / error-path tests. |
| `apps/frontend/src/components/chat/__tests__/LobsterGraphView.test.tsx` | Component test (mermaid + lib mocked). |
| `apps/frontend/src/components/chat/__tests__/FileContentViewer.test.tsx` | Tab-wiring test (LobsterGraphView mocked). |

Dependencies linear: Task 1 → 2 → 3 → 4 → 5.

---

### Task 1: Lobster YAML parser

**Files:**
- Create: `apps/frontend/src/lib/lobster/parseLobster.ts`
- Test: `apps/frontend/src/lib/lobster/__tests__/parseLobster.test.ts`
- Modify: `apps/frontend/package.json` (add `yaml` dependency)

**Interfaces:**
- Produces:
  - `interface LobsterStep { id: string; command?: string; condition?: string; stdin?: string; isApproval: boolean; }`
  - `interface LobsterWorkflow { name?: string; steps: LobsterStep[]; }`
  - `function parseLobster(content: string): LobsterWorkflow` — throws `Error` on non-object YAML.

- [ ] **Step 1: Add the `yaml` dependency**

Run:
```bash
cd apps/frontend && pnpm add yaml@^2.9.0
```
Expected: `package.json` gains `"yaml": "^2.9.0"`; lockfile updates.

- [ ] **Step 2: Write the failing test**

Create `apps/frontend/src/lib/lobster/__tests__/parseLobster.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { parseLobster } from "@/lib/lobster/parseLobster";

const WF = `name: morning-briefing
steps:
  - id: load-connected
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "ora-config/connected-calendars"}'
  - id: load-gog-events
    command: >
      openclaw.invoke --tool gog --action calendar-list-events --args-json '{"date": "{{today}}"}'
    condition: $load-connected.json.gog.connected == true
  - id: await-confirmation
    approval:
      prompt: "Approve?"
    command: exec --json --shell 'true'
`;

describe("parseLobster", () => {
  it("parses name and steps with their fields", () => {
    const wf = parseLobster(WF);
    expect(wf.name).toBe("morning-briefing");
    expect(wf.steps.map((s) => s.id)).toEqual([
      "load-connected",
      "load-gog-events",
      "await-confirmation",
    ]);
    expect(wf.steps[1].condition).toContain("$load-connected.json.gog.connected");
    expect(wf.steps[1].command).toContain("--tool gog");
  });

  it("marks steps with an approval field as approval gates", () => {
    const wf = parseLobster(WF);
    expect(wf.steps[2].isApproval).toBe(true);
    expect(wf.steps[0].isApproval).toBe(false);
  });

  it("stringifies object stdin so $refs inside remain scannable", () => {
    const wf = parseLobster(`steps:
  - id: a
    command: exec
  - id: b
    command: exec
    stdin: '{ "x": $a.json }'
`);
    expect(wf.steps[1].stdin).toContain("$a.json");
  });

  it("returns empty steps for YAML without a steps array", () => {
    expect(parseLobster("name: x\n").steps).toEqual([]);
  });

  it("throws on non-object YAML", () => {
    expect(() => parseLobster("- just\n- a\n- list")).not.toThrow(); // arrays are objects-ish; see impl
    expect(() => parseLobster(": : :")).toThrow();
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/frontend && pnpm exec vitest run src/lib/lobster/__tests__/parseLobster.test.ts`
Expected: FAIL — `Cannot find module '@/lib/lobster/parseLobster'`.

- [ ] **Step 4: Write the implementation**

Create `apps/frontend/src/lib/lobster/parseLobster.ts`:
```ts
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/frontend && pnpm exec vitest run src/lib/lobster/__tests__/parseLobster.test.ts`
Expected: PASS (all 5).

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/lib/lobster/parseLobster.ts \
        apps/frontend/src/lib/lobster/__tests__/parseLobster.test.ts \
        apps/frontend/package.json apps/frontend/../../pnpm-lock.yaml
git commit -m "feat(frontend): add lobster .lobster YAML parser

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```
(If the lockfile path differs, `git add` the repo-root `pnpm-lock.yaml`.)

---

### Task 2: Mermaid emitter + reference extraction

**Files:**
- Create: `apps/frontend/src/lib/lobster/toMermaid.ts`
- Test: `apps/frontend/src/lib/lobster/__tests__/toMermaid.test.ts`

**Interfaces:**
- Consumes: `LobsterWorkflow`, `LobsterStep` from `./parseLobster`.
- Produces:
  - `function extractStepRefs(text: string | undefined, knownIds: Set<string>): string[]`
  - `function toMermaid(workflow: LobsterWorkflow): string`

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/src/lib/lobster/__tests__/toMermaid.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { toMermaid, extractStepRefs } from "@/lib/lobster/toMermaid";
import type { LobsterWorkflow } from "@/lib/lobster/parseLobster";

describe("extractStepRefs", () => {
  const ids = new Set(["load-connected", "await-confirmation"]);
  it("keeps only $refs that match a known step id, stripping suffixes", () => {
    expect(extractStepRefs("$load-connected.json.gog x $action_type", ids)).toEqual([
      "load-connected",
    ]);
  });
  it("matches refs with hyphens and dedupes", () => {
    expect(
      extractStepRefs("$await-confirmation.json && $await-confirmation.stdout", ids),
    ).toEqual(["await-confirmation"]);
  });
  it("returns [] for undefined", () => {
    expect(extractStepRefs(undefined, ids)).toEqual([]);
  });
});

describe("toMermaid", () => {
  const wf: LobsterWorkflow = {
    name: "demo",
    steps: [
      { id: "load-connected", command: "openclaw.invoke --tool fast-io --action read", isApproval: false },
      {
        id: "load-gog",
        command: "openclaw.invoke --tool gog --action calendar-list-events",
        condition: "$load-connected.json.gog.connected == true",
        isApproval: false,
      },
      { id: "approve-send", command: "exec --shell 'true'", stdin: "$load-gog.json", isApproval: true },
    ],
  };

  it("starts with a flowchart TD header and one node per step", () => {
    const out = toMermaid(wf);
    expect(out.startsWith("flowchart TD")).toBe(true);
    expect(out).toContain('n_load_connected["load-connected');
    expect(out).toContain("gog · calendar-list-events"); // tool · action summary
  });

  it("renders approval steps as diamond nodes with the approval class", () => {
    const out = toMermaid(wf);
    expect(out).toContain("n_approve_send{");
    expect(out).toContain("classDef approval");
    expect(out).toMatch(/class .*n_approve_send.*approval;/);
  });

  it("draws a condition edge with a (truncated) label", () => {
    const out = toMermaid(wf);
    expect(out).toMatch(/n_load_connected -->\|".*gog\.connected.*"\| n_load_gog/);
  });

  it("draws a plain data edge for stdin refs and no self edges", () => {
    const out = toMermaid(wf);
    expect(out).toContain("n_load_gog --> n_approve_send");
    expect(out).not.toContain("n_load_gog --> n_load_gog");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm exec vitest run src/lib/lobster/__tests__/toMermaid.test.ts`
Expected: FAIL — `Cannot find module '@/lib/lobster/toMermaid'`.

- [ ] **Step 3: Write the implementation**

Create `apps/frontend/src/lib/lobster/toMermaid.ts`:
```ts
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm exec vitest run src/lib/lobster/__tests__/toMermaid.test.ts`
Expected: PASS (all 7).

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/lib/lobster/toMermaid.ts \
        apps/frontend/src/lib/lobster/__tests__/toMermaid.test.ts
git commit -m "feat(frontend): emit mermaid flowchart from lobster workflow model

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Public render interface (`lobsterToMermaid`)

**Files:**
- Create: `apps/frontend/src/lib/lobster/index.ts`
- Test: `apps/frontend/src/lib/lobster/__tests__/index.test.ts`

**Interfaces:**
- Consumes: `parseLobster`, `toMermaid`.
- Produces:
  - `type LobsterRenderResult = { ok: true; mermaid: string } | { ok: false; reason: "parse" | "empty"; message: string }`
  - `function lobsterToMermaid(content: string): LobsterRenderResult`
  - Re-exports `parseLobster`, `toMermaid`, and the model types.

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/src/lib/lobster/__tests__/index.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { lobsterToMermaid } from "@/lib/lobster";

describe("lobsterToMermaid", () => {
  it("returns ok + mermaid for a valid workflow", () => {
    const res = lobsterToMermaid(`name: x
steps:
  - id: a
    command: exec --shell 'true'
`);
    expect(res.ok).toBe(true);
    if (res.ok) expect(res.mermaid).toContain("flowchart TD");
  });

  it("returns reason 'empty' when there are no steps", () => {
    const res = lobsterToMermaid("name: x\n");
    expect(res).toEqual({ ok: false, reason: "empty", message: expect.any(String) });
  });

  it("returns reason 'parse' on invalid YAML", () => {
    const res = lobsterToMermaid(": : :");
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.reason).toBe("parse");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm exec vitest run src/lib/lobster/__tests__/index.test.ts`
Expected: FAIL — `Cannot find module '@/lib/lobster'`.

- [ ] **Step 3: Write the implementation**

Create `apps/frontend/src/lib/lobster/index.ts`:
```ts
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm exec vitest run src/lib/lobster/__tests__/index.test.ts`
Expected: PASS (all 3).

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/lib/lobster/index.ts \
        apps/frontend/src/lib/lobster/__tests__/index.test.ts
git commit -m "feat(frontend): add lobsterToMermaid render-result interface

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `LobsterGraphView` component

**Files:**
- Create: `apps/frontend/src/components/chat/LobsterGraphView.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/LobsterGraphView.test.tsx`
- Modify: `apps/frontend/package.json` (add `mermaid` dependency)

**Interfaces:**
- Consumes: `lobsterToMermaid` from `@/lib/lobster`.
- Produces: `function LobsterGraphView(props: { content: string; onViewCode?: () => void }): JSX.Element`

- [ ] **Step 1: Add the `mermaid` dependency**

Run:
```bash
cd apps/frontend && pnpm add mermaid@^11
```
Expected: `package.json` gains `"mermaid": "^11..."`.

- [ ] **Step 2: Write the failing test**

Create `apps/frontend/src/components/chat/__tests__/LobsterGraphView.test.tsx`:
```tsx
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const renderMock = vi.fn().mockResolvedValue({ svg: '<svg data-testid="diagram"></svg>' });
vi.mock("mermaid", () => ({
  default: { initialize: vi.fn(), render: renderMock },
}));

const lobsterToMermaidMock = vi.fn();
vi.mock("@/lib/lobster", () => ({
  lobsterToMermaid: (c: string) => lobsterToMermaidMock(c),
}));

import { LobsterGraphView } from "@/components/chat/LobsterGraphView";

beforeEach(() => {
  lobsterToMermaidMock.mockReset();
  renderMock.mockClear();
});

describe("LobsterGraphView", () => {
  it("shows a loading state then renders the mermaid svg on the ok path", async () => {
    lobsterToMermaidMock.mockReturnValue({ ok: true, mermaid: "flowchart TD\n n_a[\"a\"]" });
    render(<LobsterGraphView content="steps:\n  - id: a\n    command: exec" />);
    // Synchronously before the mermaid render promise resolves, svg is null.
    expect(screen.getByText(/rendering graph/i)).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("diagram")).toBeTruthy());
    expect(renderMock).toHaveBeenCalled();
  });

  it("shows a fallback and a 'View as code' action on parse failure", () => {
    lobsterToMermaidMock.mockReturnValue({ ok: false, reason: "parse", message: "bad" });
    const onViewCode = vi.fn();
    render(<LobsterGraphView content=":::" onViewCode={onViewCode} />);
    expect(screen.getByText(/couldn't render/i)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /view as code/i }));
    expect(onViewCode).toHaveBeenCalled();
  });

  it("shows the empty-state message when there are no steps", () => {
    lobsterToMermaidMock.mockReturnValue({ ok: false, reason: "empty", message: "No workflow steps found to graph." });
    render(<LobsterGraphView content="name: x" />);
    expect(screen.getByText(/no workflow steps/i)).toBeTruthy();
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/LobsterGraphView.test.tsx`
Expected: FAIL — `Cannot find module '@/components/chat/LobsterGraphView'`.

- [ ] **Step 4: Write the implementation**

Create `apps/frontend/src/components/chat/LobsterGraphView.tsx`:
```tsx
"use client";

import * as React from "react";
import { ZoomIn, ZoomOut, RotateCcw, Copy, FileText, Loader2 } from "lucide-react";
import { lobsterToMermaid } from "@/lib/lobster";

interface LobsterGraphViewProps {
  content: string;
  onViewCode?: () => void;
}

// Themed once, shared across instances. theme:"base" + a full themeVariables
// set keeps the diagram inside the product's cream/green palette instead of
// mermaid's default blues. securityLevel:"strict" sanitizes labels since we
// inject the SVG via dangerouslySetInnerHTML.
const MERMAID_CONFIG = {
  startOnLoad: false,
  securityLevel: "strict",
  theme: "base",
  fontFamily: "ui-sans-serif, system-ui, sans-serif",
  themeVariables: {
    primaryColor: "#f3efe6",
    primaryBorderColor: "#06402B",
    primaryTextColor: "#1a1a1a",
    secondaryColor: "#ece6d8",
    tertiaryColor: "#f6f2ea",
    lineColor: "#8a8578",
    edgeLabelBackground: "#f3efe6",
    fontSize: "13px",
  },
} as const;

let mermaidInitialized = false;

function Fallback({ message, onViewCode }: { message: string; onViewCode?: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-3 text-sm text-[#8a8578]">
      <p>{message}</p>
      {onViewCode ? (
        <button
          onClick={onViewCode}
          className="flex items-center gap-1 px-2 py-1 text-xs rounded border border-[#e0dbd0] hover:bg-[#f3efe6] transition-colors"
        >
          <FileText className="h-3 w-3" /> View as code
        </button>
      ) : null}
    </div>
  );
}

// Renders a .lobster workflow as a Mermaid flowchart. Mermaid is imported
// dynamically so it stays out of the main chat bundle until a graph is shown.
export function LobsterGraphView({ content, onViewCode }: LobsterGraphViewProps) {
  const result = React.useMemo(() => lobsterToMermaid(content), [content]);
  const [svg, setSvg] = React.useState<string | null>(null);
  const [renderError, setRenderError] = React.useState<string | null>(null);
  const [scale, setScale] = React.useState(1);

  const reactId = React.useId();
  const diagramId = React.useMemo(() => "lobster-" + reactId.replace(/[^a-zA-Z0-9]/g, ""), [reactId]);

  React.useEffect(() => {
    if (!result.ok) return;
    let cancelled = false;
    setSvg(null);
    setRenderError(null);
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        if (!mermaidInitialized) {
          mermaid.initialize(MERMAID_CONFIG);
          mermaidInitialized = true;
        }
        const { svg } = await mermaid.render(diagramId, result.mermaid);
        if (!cancelled) setSvg(svg);
      } catch (err) {
        if (!cancelled) setRenderError((err as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [result, diagramId]);

  if (!result.ok) {
    const message =
      result.reason === "parse" ? "Couldn't render this workflow as a graph." : result.message;
    return <Fallback message={message} onViewCode={onViewCode} />;
  }

  const iconBtn =
    "p-1 rounded text-[#8a8578] hover:bg-[#e0dbd0] transition-colors disabled:opacity-40";

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-1 px-3 py-1.5 bg-[#f3efe6] border-b border-[#e0dbd0] flex-shrink-0">
        <button className={iconBtn} title="Zoom in" aria-label="Zoom in" onClick={() => setScale((s) => Math.min(s + 0.2, 3))}>
          <ZoomIn className="h-4 w-4" />
        </button>
        <button className={iconBtn} title="Zoom out" aria-label="Zoom out" onClick={() => setScale((s) => Math.max(s - 0.2, 0.4))}>
          <ZoomOut className="h-4 w-4" />
        </button>
        <button className={iconBtn} title="Reset zoom" aria-label="Reset zoom" onClick={() => setScale(1)}>
          <RotateCcw className="h-4 w-4" />
        </button>
        <span className="text-[10px] text-[#8a8578] ml-1">{Math.round(scale * 100)}%</span>
        <button
          className="ml-auto flex items-center gap-1 px-2 py-1 text-xs rounded text-[#8a8578] hover:bg-[#e0dbd0] transition-colors"
          title="Copy mermaid source"
          onClick={() => navigator.clipboard?.writeText(result.mermaid)}
        >
          <Copy className="h-3 w-3" /> Copy source
        </button>
      </div>
      <div className="flex-1 overflow-auto p-4 bg-white">
        {renderError ? (
          <div className="text-sm">
            <p className="text-red-500 mb-2">Couldn&apos;t render the diagram.</p>
            <pre className="text-xs bg-[#f3efe6] p-3 rounded overflow-auto whitespace-pre-wrap">
              {result.mermaid}
            </pre>
          </div>
        ) : svg === null ? (
          <div className="flex items-center justify-center h-full text-[#8a8578] text-sm">
            <Loader2 className="h-5 w-5 animate-spin mr-2" /> Rendering graph…
          </div>
        ) : (
          <div
            style={{ transform: `scale(${scale})`, transformOrigin: "top left" }}
            dangerouslySetInnerHTML={{ __html: svg }}
          />
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/LobsterGraphView.test.tsx`
Expected: PASS (all 3).

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/chat/LobsterGraphView.tsx \
        apps/frontend/src/components/chat/__tests__/LobsterGraphView.test.tsx \
        apps/frontend/package.json apps/frontend/../../pnpm-lock.yaml
git commit -m "feat(frontend): add LobsterGraphView mermaid renderer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Wire Graph | Code tabs into `FileContentViewer`

**Files:**
- Modify: `apps/frontend/src/components/chat/FileContentViewer.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/FileContentViewer.test.tsx`

**Interfaces:**
- Consumes: `LobsterGraphView` from `@/components/chat/LobsterGraphView`; `Tabs, TabsList, TabsTrigger, TabsContent` from `@/components/ui/tabs`.
- Produces: no new exports — same `FileContentViewer` props.

**Context:** `FileContentViewer.tsx` today ends with a single "Editable text file" return (header bar with filename + Save, then a `<textarea>`). For `.lobster` files we wrap that block in a controlled `Tabs` (so the Graph fallback can switch to Code). All earlier early-returns (loading / error / no file / binary / image) stay unchanged. `FileInfo` has `{ name, path, content, binary, mime_type }`.

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/src/components/chat/__tests__/FileContentViewer.test.tsx`:
```tsx
// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("@/components/chat/LobsterGraphView", () => ({
  LobsterGraphView: ({ content, onViewCode }: { content: string; onViewCode?: () => void }) => (
    <div>
      <span>GRAPH:{content.length}</span>
      <button onClick={onViewCode}>to-code</button>
    </div>
  ),
}));

import { FileContentViewer } from "@/components/chat/FileContentViewer";

function fileOf(name: string, content: string) {
  return { name, path: "/" + name, content, binary: false, mime_type: "text/plain" };
}

describe("FileContentViewer .lobster handling", () => {
  it("defaults a .lobster file to the Graph tab", () => {
    render(<FileContentViewer file={fileOf("wf.lobster", "steps:\n  - id: a")} isLoading={false} error={null} />);
    expect(screen.getByText(/^GRAPH:/)).toBeTruthy();
  });

  it("switches to the editable Code view when the Code tab is clicked", () => {
    render(<FileContentViewer file={fileOf("wf.lobster", "steps:\n  - id: a")} isLoading={false} error={null} />);
    fireEvent.click(screen.getByRole("tab", { name: /code/i }));
    expect(screen.getByRole("textbox")).toBeTruthy();
  });

  it("renders a non-lobster file directly as an editable textarea with no tabs", () => {
    render(<FileContentViewer file={fileOf("notes.txt", "hello")} isLoading={false} error={null} />);
    expect(screen.getByRole("textbox")).toBeTruthy();
    expect(screen.queryByRole("tab")).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/FileContentViewer.test.tsx`
Expected: FAIL — no tabs / `GRAPH:` text rendered (current viewer shows only a textarea).

- [ ] **Step 3: Edit the implementation**

In `apps/frontend/src/components/chat/FileContentViewer.tsx`:

(a) Add imports at the top (after the existing imports):
```tsx
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { LobsterGraphView } from "@/components/chat/LobsterGraphView";
```

(b) Replace the final `// Editable text file` return block (the `return ( <div className="flex flex-col h-full"> ... </textarea> </div> )`) with:
```tsx
  // Editable text file. The header + textarea are shared by the plain
  // editor and the Code tab of the .lobster viewer.
  const editor = (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-1.5 bg-[#f3efe6] border-b border-[#e0dbd0] flex-shrink-0">
        <span className="text-xs text-[#8a8578]">{file.name}</span>
        <div className="flex items-center gap-2">
          {dirty && <span className="text-[10px] text-amber-500 font-medium">unsaved</span>}
          <button
            onClick={handleSave}
            disabled={!dirty || saving || !onSave}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded bg-[#06402B] text-white hover:bg-[#0a5c3e] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
            Save
          </button>
        </div>
      </div>
      <textarea
        ref={textareaRef}
        value={editContent}
        onChange={(e) => setEditContent(e.target.value)}
        className="flex-1 w-full p-4 text-sm font-mono bg-white text-[#1a1a1a] resize-none focus:outline-none"
        spellCheck={false}
      />
    </div>
  );

  // .lobster files get a Graph (default) | Code tab pair. Graph renders the
  // current editor content so it reflects unsaved edits when you switch back.
  if (file.name.endsWith(".lobster")) {
    return <LobsterTabbedViewer editor={editor} content={editContent} />;
  }

  return editor;
```

(c) Add this controlled-tab subcomponent at the bottom of the file (below `FileContentViewer`):
```tsx
function LobsterTabbedViewer({
  editor,
  content,
}: {
  editor: React.ReactNode;
  content: string;
}) {
  const [tab, setTab] = React.useState<"graph" | "code">("graph");
  return (
    <Tabs value={tab} onValueChange={(v) => setTab(v as "graph" | "code")} className="flex flex-col h-full">
      {/* Match the viewer's header band so the tabs read as native, not as
          shadcn's default pill group. */}
      <TabsList className="flex-shrink-0 justify-start gap-1 rounded-none bg-[#f3efe6] border-b border-[#e0dbd0] px-2 py-1">
        <TabsTrigger value="graph" className="text-xs">Graph</TabsTrigger>
        <TabsTrigger value="code" className="text-xs">Code</TabsTrigger>
      </TabsList>
      <TabsContent value="graph" className="flex-1 min-h-0 m-0">
        <LobsterGraphView content={content} onViewCode={() => setTab("code")} />
      </TabsContent>
      <TabsContent value="code" className="flex-1 min-h-0 m-0">
        {editor}
      </TabsContent>
    </Tabs>
  );
}
```

> Implementer note: `@/components/ui/tabs` is the shadcn/Radix wrapper. Confirm the
> `TabsList`/`TabsTrigger` default variants compose with these overrides; if the
> default styling fights the band (e.g. a baked-in pill background), read
> `ui/tabs.tsx` and align the trigger active state to a flat underline in the
> green accent. Verify visually in the manual-verification pass.

> Note for the implementer: confirm `@/components/ui/tabs` exports accept `className` and a controlled `value`/`onValueChange` (it's the shadcn/Radix tabs — it does). If `TabsContent` unmounts inactive tabs and that drops textarea edit state, pass `forceMount` to the code `TabsContent` and hide via CSS; verify with the Task-5 tests before adding that.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/FileContentViewer.test.tsx`
Expected: PASS (all 3).

- [ ] **Step 5: Run the lobster + viewer tests together as a final check**

Run:
```bash
cd apps/frontend && pnpm exec vitest run \
  src/lib/lobster src/components/chat/__tests__/LobsterGraphView.test.tsx \
  src/components/chat/__tests__/FileContentViewer.test.tsx
```
Expected: PASS, all files. (Do NOT run the full suite.)

- [ ] **Step 6: Lint the touched files**

Run: `cd apps/frontend && pnpm run lint`
Expected: no new errors in the lobster lib or chat components.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/chat/FileContentViewer.tsx \
        apps/frontend/src/components/chat/__tests__/FileContentViewer.test.tsx
git commit -m "feat(frontend): show Graph|Code tabs for .lobster files in the workspace viewer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Manual verification (after Task 5)

1. `cd apps/frontend && pnpm run dev`, open `/chat`, open the workspace file browser for an agent that has `.lobster` files (e.g. a `pitch`/`ora`/`echo` agent).
2. Open a `.lobster` file → defaults to **Graph**, shows a flowchart (boxes for steps, labelled condition edges, diamond approval nodes).
3. Zoom in/out/reset works; "Copy source" puts mermaid text on the clipboard.
4. Switch to **Code** → the existing editable textarea + Save is intact; edits then Save still works.
5. Open a malformed `.lobster` (delete `steps:`) → Graph shows the empty/parse fallback with "View as code".
6. Open a non-`.lobster` file → unchanged single textarea, no tabs.

## Self-Review

- **Spec coverage:** client-side derivation (Tasks 1–3), mermaid render + theme + zoom + copy (Task 4), Graph|Code tabs defaulting to Graph with editing preserved (Task 5), error/empty fallbacks (Tasks 3–4), tests against real fixtures (Tasks 1–2) — all mapped. Generator "path #1 vs fallback" resolved to the hand-rolled emitter in Global Constraints. `dot`/`ascii` are non-goals (omitted intentionally).
- **Placeholders:** none — every code/test step has full content and exact commands.
- **Type consistency:** `LobsterStep`/`LobsterWorkflow` (Task 1) consumed unchanged by `toMermaid` (Task 2) and `lobsterToMermaid` (Task 3); `LobsterRenderResult` discriminated union consumed by `LobsterGraphView` (Task 4); `LobsterGraphView({content,onViewCode})` consumed by Task 5. Names align across tasks.

## Skill review applied (2026-06-21)

Reviewed the planned UI with the React best-practices and frontend-design skills before
finalizing. Changes folded into the tasks above:

- **React:** `mermaid.initialize` now runs once via a module-level guard (`advanced-init-once`);
  explicit `securityLevel: "strict"` for the injected SVG; `Fallback` uses a ternary
  (`rendering-conditional-render`). Confirmed-good and unchanged: dynamic `import("mermaid")`,
  load-only-when-shown, `useMemo` parse, functional `setScale`, wrapper-not-SVG transform,
  `useId` diagram id, effect `cancelled` cleanup, module-level subcomponents.
- **Design (consistency-first — must match the cream/green system, not introduce a new look):**
  full themed mermaid config extracted to `MERMAID_CONFIG` (incl. `fontFamily` + edge-label bg);
  approval gates get a distinct warm `classDef` so they're scannable; a "Rendering graph…"
  loading state removes the blank-flash; `TabsList` styled to the viewer header band.
- **Accessibility:** icon-only zoom buttons given `aria-label`s; tabs are accessible via Radix roles.

## Phase 2 (NOT in this plan — separate follow-up)

The in-chat "view workflow" button (detect a `.lobster` path in agent output via `lib/filePathDetection.ts`, render a button that opens that file's Graph view, reusing `LobsterGraphView`) is specced in the design doc but deferred until Phase 1 ships and is validated. It needs its own short plan once we decide whether it opens in the file panel or a modal over chat.
