# Documents (Track C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A document an agent writes reads like a colleague's memo, opens in the right pane with its author, date, version and a contents list, renders cards and diagrams, and a person can comment on a passage, see and restore versions, and edit it in a real editor (web) or a plain one (phone).

**Architecture:** Three independent levers. (1) Writing rules: a new `references/documents.md` in the bundled lightbulb skill, the first document style guide agents have had. (2) The pane: the artifact pane's document branch (Track B) mounts the already-ported `DocumentRow`, which carries the annotation layer and panel, the revision menu with historical preview, restore and diff; this plan adds a header, a contents list, an Edit action, and makes document bodies render through the room markdown overrides so cards and mermaid draw read-only. (3) Editing: a lean MDXEditor-based editor (not a port of the fork's 1,671-line component, whose bulk is Paperclip-specific mention and slash-command machinery) saving a new revision through the existing `PUT /teams/issues/{issue}/documents/{key}` with `baseRevisionId`, and a conflict path when the document moved; the phone gets a plain editor on the same write.

**Tech Stack:** `@mdxeditor/editor` 4.3.3 (lazy-loaded; lexical + codemirror inside), react-markdown 10 with the room overrides, vitest + Testing Library (web), jest-expo (phone), pytest (skill build check).

**Spec:** `docs/superpowers/specs/2026-10-16-generative-ui-and-artifacts-design.md`, sections C1–C4 and the "C. Documents" mechanisms. Open decision 6 is settled: the Paperclip fork re-wakes the task's assignee on a document annotation comment (`issue.document_annotation_comment_added` is in its rewake-event list), so comments reach the agent like a task comment does.

**Depends on:** Track A's PR (the `roomMarkdown` overrides and `CardContext`) and Track B's PR (the `artifact` pane with its `DocumentPane` branch) merged to `main`. Branch from `origin/main` after both land.

## Global Constraints

- The writing rules live in the lightbulb skill (`apps/infra/hermes/skills-src/lightbulb/references/documents.md`), routed from `SKILL.md`'s table; the paperclip skill's `comment-style.md` keeps governing comments only. `SKILL.md` is near its budget: the routing row is at most 200 characters. A skill change ships only through the image build → dev pin → prod pin → runtime roll → `reapply_hermes_skills_dirs.py --all`; if Track A's skill PR has not merged yet, add `documents.md` to that PR instead of opening a second one.
- Documents render through `MarkdownView` with the room overrides (`roomMarkdown`) inside a READ-ONLY `CardContext` (no `onReply`): cards and diagrams draw, nothing can post. A person's pasted fence in a document therefore renders as a disabled card, never a live one.
- Every write goes through `PUT /teams/issues/{issue}/documents/{key}` with `{ title, format: "markdown", body, changeSummary, baseRevisionId }` where `baseRevisionId` is the revision the person opened (`latestRevisionId` on the document payload). A non-2xx answer whose body says conflict (upstream answers a user's stale or locked save with its conflict error) reloads the document and says who changed it; the draft is kept on screen until the person discards it.
- The editor is lazy-loaded; `@mdxeditor/editor` never enters the /chat chunk. Its stylesheet is imported in the editor module only and themed with our tokens; no colour literals outside the scanner's exemptions.
- The phone editor is a plain multiline text field; the rich editor is web-only.
- Branch each PR from `origin/main` in its own worktree under `lightbulb.nosync/.claude/worktrees/`. Each PR: CI green and Codex reviewed before merge. Dev acceptance in Chrome with receipts on the PR; phone flows in the simulator.
- Every commit message ends with a blank line and then exactly `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. **Two people editing the same document.** The second save must not silently overwrite the first: the conflict path shows who changed it and keeps the draft (Task 4 test "a stale save keeps the draft and names the other editor").
2. **An agent run holding the document's lock.** A person's save while a run is writing the same document gets the same conflict path, not a 500 or a lost edit (Task 4 test covers the conflict shape; Task 6 acceptance step 4).
3. **A document with a card fence written by a person.** It renders as a disabled card: no button posts, no `onReply` exists (Task 2 test "a card in a document is read-only").
4. **A very long document.** The contents list appears at four or more headings and scrolls to the heading; a 200 KB body still opens without freezing (Task 3 test "contents list from headings"; body size is bounded upstream at 512 KB).
5. **Save on a slow network.** Double-pressing Save sends one PUT; the button disables while in flight and the draft survives a failure (Task 4 test "one save in flight").

---

### Task 1: The writing rules in the skill

**Files:**
- Create: `apps/infra/hermes/skills-src/lightbulb/references/documents.md`
- Modify: `apps/infra/hermes/skills-src/lightbulb/SKILL.md` (one routing row)
- Modify: `apps/infra/hermes/skills-src/lightbulb/references/chat-vs-document.md` (one pointer in step 2, "Write the document")
- Modify: `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md` (anchor)
- Test: `apps/backend/tests/unit/services/test_hermes_documents_skill.py`

**Interfaces:**
- Produces: `references/documents.md` in the built skills tree; a `SKILL.md` row pointing at it.

- [ ] **Step 1: Write the failing test**

`apps/backend/tests/unit/services/test_hermes_documents_skill.py`:

```python
"""The lightbulb skill routes document writing to references/documents.md, and the built tree ships it."""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
BUILD = REPO / "apps/infra/hermes/build-skills.sh"


def test_the_skill_routes_document_writing_to_the_documents_reference(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(BUILD), str(out)], check=True, capture_output=True)
    skill = (out / "lightbulb/SKILL.md").read_text()
    row = next(line for line in skill.splitlines() if "`references/documents.md`" in line)
    assert row.startswith("|") and len(row) <= 200
    doc = (out / "lightbulb/references/documents.md").read_text()
    for phrase in ("Open with the answer", "Observed", "table", "who does what by when", "request_approval"):
        assert phrase in doc, phrase
    cvd = (out / "lightbulb/references/chat-vs-document.md").read_text()
    assert "references/documents.md" in cvd
    assert "documents.md" in (REPO / "apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md").read_text()
```

Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_documents_skill.py -v` → FAIL (no row, no file).

- [ ] **Step 2: Write the reference**

`references/documents.md`:

```markdown
# Writing a document

A document is read by a person who did not watch you work. It is a memo from a colleague, not
a status report. The pane shows the title, who wrote it and when, and the version: never
repeat those in the body.

## Shape

- **Open with the answer or the decision**, in one paragraph. Not a timestamp, not the title
  as a heading, not "Observed 2026-10-16 at 20:27 UTC", not "Current-state audit".
- **Say who does what by when.** A plan without an owner and a date is a wish.
- **Name tasks in a table**, with their links (`[HAB-12](/HAB/issues/HAB-12)`), never as a
  list of ids. A table is for comparing things; prose is for reasoning.
- **Headings only in a document longer than one screen**, and then at most three levels.
- **Numbers and plans as cards.** A card fence (`references/ui.md`) works in a document:
  `Metric` for a number, `Table` for a comparison, `Steps` for a plan, a chart only when the
  shape of the data is the point. A `mermaid` fence draws a diagram.
- **No approvals inside a document.** A sign-off is the `request_approval` tool, linked to the
  document's task.

## What each kind carries

| Kind | Sections, in this order |
|---|---|
| Plan | The goal in one line; what will be done; who does each part, by when; what is needed from the reader |
| Research | The answer; the evidence (sources as a `Sources` card or links); what to do about it |
| Report | What happened; what it means; what happens next |
| Spec / proposal | What changes for the person; how it works; what it costs; what is not included |

## Length

A page is enough for most things. Past three screens, split: a document per question, linked
from the first.

## Updating

Edit the same document (`GET` it, pass its `latestRevisionId` as `baseRevisionId`) rather than
writing "v2" beside it. Write a one-line `changeSummary` the person will read in the version
menu ("Moved the launch to Nov 4; dropped the webinar.").
```

- [ ] **Step 3: Route it**

In `SKILL.md`'s "Where to read the detail" table, after the `references/chat-vs-document.md` row, add:

```markdown
| `references/documents.md` | **How to write a document** (a plan, research, a report, a spec): open with the answer, owners and dates, tasks in a table, cards and diagrams. |
```

In `chat-vs-document.md`, step 2 ("**Write the document.**"), add after the first sentence: `Shape and voice: \`references/documents.md\`.`

In `MAINTENANCE.md`, under the anchors, add:

```text
  - Writing a document ...... references/documents.md
                              <- owner complaint 2026-10-16 (a GTM document opened with "Observed … UTC",
                                 "Decision", "Current-state audit" and task-id bullets); the pane header now carries
                                 title/author/date/version so the body never repeats them; cards via references/ui.md.
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_documents_skill.py tests/unit/services/test_hermes_skill_tool_names.py -v && wc -c ../infra/hermes/skills-src/lightbulb/SKILL.md`
Expected: PASS; `SKILL.md` grew by at most 200 characters.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/hermes/skills-src/lightbulb apps/backend/tests/unit/services/test_hermes_documents_skill.py
git commit -m "skill: how to write a document (references/documents.md)"
```

Ship: with Track A's skill PR if it is still open; otherwise as its own PR, then image build → dev pin → prod pin → roll → `reapply_hermes_skills_dirs.py --all` (Track A's plan, Task 9, has the commands).

---

### Task 2: Documents render cards and diagrams, read-only

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/DocumentRow.tsx` (the `<MarkdownView>` at ~line 295)
- Test: `apps/frontend/src/components/teams/issues/__tests__/DocumentRow.cards.test.tsx`

**Interfaces:**
- Consumes: `roomMarkdown` (`components/messaging/roomMarkdown.tsx`), `CardContext` / `CardContextValue` (`components/messaging/openui/CardContext.tsx`).
- Produces: a document body renders ```` ```openui-lang ```` and ```` ```mermaid ```` fences; cards have no reply path.

- [ ] **Step 1: Write the failing test**

```tsx
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { DocumentRow } from "../DocumentRow";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: () => ({ data: undefined, mutate: vi.fn() }), post: vi.fn() }) }));

const doc = {
  id: "d1", key: "plan", title: "Launch plan", format: "markdown", updatedAt: "2026-10-16T10:00:00Z",
  body: "Pick one:\n\n```openui-lang\nroot = Stack([q])\nq = Choice(\"Which first?\", [\"Starter\", \"Studio\"])\n```\n",
};

it("a card in a document is read-only", async () => {
  render(<DocumentRow doc={doc} agents={[]} issueId="i1" defaultOpen />);
  const button = await screen.findByRole("button", { name: "Studio" });
  expect(button).toBeDisabled();
  expect(screen.queryByText(/root = Stack/)).toBeNull();
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/teams/issues/__tests__/DocumentRow.cards.test.tsx`
Expected: FAIL (`defaultOpen` unknown; the fence renders as code).

- [ ] **Step 3: Implement**

In `DocumentRow.tsx`:
- Add a prop `defaultOpen?: boolean` and initialise `const [open, setOpen] = useState(Boolean(defaultOpen));`.
- Build a read-only card context once per document: `const readOnlyCards = useMemo<CardContextValue>(() => ({ messageId: doc.id, roomId: "", authorId: "", threadRootMessageId: null, attachments: [], answered: null }), [doc.id]);`
- Replace `<MarkdownView>{shownBody}</MarkdownView>` with `<CardContext.Provider value={readOnlyCards}><MarkdownView skipHtml components={roomMarkdown}>{shownBody}</MarkdownView></CardContext.Provider>`.
- Keep `bodyRef` wrapping exactly that block (the annotation layer walks its text nodes; a card's text is part of the body, which is fine).

- [ ] **Step 4: Run the tests**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/teams/issues/__tests__/DocumentRow.cards.test.tsx src/components/teams/issues/__tests__/DocumentRow.restore.test.tsx src/components/teams/issues/__tests__/DocumentReview.test.tsx && pnpm -F @lightbulb/frontend exec tsc --noEmit -p tsconfig.json && pnpm -F @lightbulb/frontend lint`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/teams/issues
git commit -m "documents: cards and diagrams render read-only inside a document body"
```

---

### Task 3: The document view in the pane: header, contents list, Open task

**Files:**
- Modify: `apps/frontend/src/components/workspace/panels/ArtifactDockPanel.tsx` (the `DocumentPane` branch from Track B)
- Create: `apps/frontend/src/components/workspace/panels/DocumentContents.tsx`
- Create: `apps/frontend/src/components/workspace/panels/documentOutline.ts`
- Test: `apps/frontend/src/components/workspace/panels/__tests__/documentOutline.test.ts`
- Test: `apps/frontend/src/components/workspace/panels/__tests__/DocumentPane.test.tsx`

**Interfaces:**
- Consumes: `DocumentRow` with `defaultOpen` (Task 2); `useTeamsApi().read` for `/issues/{issue}/documents/{key}` (its payload carries `latestRevisionId`, `latestRevisionNumber`, `updatedByAgentId`, `updatedByUserId`, `updatedAt`), `/agents` (names), and `/members` (names).
- Produces: `outlineOf(markdown): { level: 1|2|3; text: string }[]` (pure; ignores headings inside fenced code), `DocumentContents` (a list that scrolls the matching heading in a container into view), and the header line "`<author>` · updated `<time ago>` · rev `<n>`".

- [ ] **Step 1: Write the failing tests**

`documentOutline.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { outlineOf } from "../documentOutline";

describe("outlineOf", () => {
  it("lists h1–h3 in order and skips headings inside fences", () => {
    const md = "# Plan\n\ntext\n\n## Goal\n\n```md\n# not a heading\n```\n\n### Week 1\n\n#### too deep\n";
    expect(outlineOf(md)).toEqual([{ level: 1, text: "Plan" }, { level: 2, text: "Goal" }, { level: 3, text: "Week 1" }]);
  });
});
```

`DocumentPane.test.tsx`:

```tsx
import { render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const reads: Record<string, unknown> = {};
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: (path: string | null) => ({ data: path ? reads[path] : undefined, mutate: vi.fn() }), post: vi.fn(), download: vi.fn() }) }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ download: vi.fn() }) }));
vi.mock("@/components/workspace/workspaceController", () => ({ closePane: vi.fn(), openPanel: vi.fn() }));
import { ArtifactDockPanel } from "../ArtifactDockPanel";

const body = ["# Launch plan", "", "Open with the answer.", "", "## Goal", "", "x", "", "## Who", "", "y", "", "## When", "", "z", "", "## Risks", "", "w"].join("\n");

it("shows author, date and version, and a contents list from headings", async () => {
  reads["/issues/i1/documents/plan"] = { id: "d1", key: "plan", title: "Launch plan", body, updatedByAgentId: "a1", updatedAt: "2026-10-16T10:00:00Z", latestRevisionId: "r3", latestRevisionNumber: 3 };
  reads["/agents"] = [{ id: "a1", name: "Growth" }];
  reads["/members"] = [];
  render(<ArtifactDockPanel id="artifact" params={{ src: "paperclip", kind: "document", issue: "i1", key: "plan", ident: "ISOA-850" }} />);
  expect(await screen.findByRole("heading", { name: "Launch plan" })).toBeInTheDocument();
  expect(screen.getByText(/Growth · updated .* · rev 3/)).toBeInTheDocument();
  const contents = screen.getByRole("navigation", { name: "Contents" });
  expect(within(contents).getAllByRole("button").map((b) => b.textContent)).toEqual(["Launch plan", "Goal", "Who", "When", "Risks"]);
  expect(screen.getByRole("button", { name: /open task/i })).toBeInTheDocument();
});

it("hides the contents list under four headings", async () => {
  reads["/issues/i1/documents/short"] = { id: "d2", key: "short", title: "Short", body: "# A\n\n## B\n\ntext", updatedAt: "2026-10-16T10:00:00Z", latestRevisionNumber: 1 };
  render(<ArtifactDockPanel id="artifact" params={{ src: "paperclip", kind: "document", issue: "i1", key: "short" }} />);
  await screen.findByRole("heading", { name: "Short" });
  expect(screen.queryByRole("navigation", { name: "Contents" })).toBeNull();
});
```

If `/agents` / `/members` are read with a different path in the Teams hooks (`grep -rn "read<CompanyAgent" apps/frontend/src/components/teams | head`), use those paths in both the component and the test.

- [ ] **Step 2: Run them to see them fail**, then **Step 3: Implement**

`documentOutline.ts`:

```ts
export interface OutlineEntry { level: 1 | 2 | 3; text: string }

/** h1–h3 headings in order, ignoring fenced code. The contents list is shown at four or more. */
export function outlineOf(markdown: string): OutlineEntry[] {
  const out: OutlineEntry[] = [];
  let fenced = false;
  for (const raw of markdown.split("\n")) {
    const line = raw.trimEnd();
    if (/^\s*(```|~~~)/.test(line)) { fenced = !fenced; continue; }
    if (fenced) continue;
    const m = /^(#{1,3})\s+(.+?)\s*#*$/.exec(line);
    if (m) out.push({ level: m[1].length as 1 | 2 | 3, text: m[2].replace(/[*_`]/g, "").trim() });
  }
  return out;
}

export const CONTENTS_MIN_HEADINGS = 4;
```

`DocumentContents.tsx`:

```tsx
"use client";

import type { RefObject } from "react";
import type { OutlineEntry } from "./documentOutline";

/** Scrolls the heading with the same text into view; no ids are injected into the rendered body. */
export function DocumentContents({ entries, bodyRef }: { entries: OutlineEntry[]; bodyRef: RefObject<HTMLElement | null> }) {
  const go = (entry: OutlineEntry) => {
    const headings = bodyRef.current?.querySelectorAll<HTMLElement>("h1, h2, h3") ?? [];
    const target = Array.from(headings).find((h) => (h.textContent ?? "").trim() === entry.text);
    target?.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  return (
    <nav aria-label="Contents" className="mb-3 rounded-sm px-3 py-2 text-caption" style={{ background: "var(--plane-raised)" }}>
      <ul className="grid gap-1">
        {entries.map((e, i) => <li key={i} style={{ paddingLeft: `${(e.level - 1) * 12}px` }}>
          <button type="button" className="text-left hover:underline" style={{ color: "var(--fg-soft)" }} onClick={() => go(e)}>{e.text}</button>
        </li>)}
      </ul>
    </nav>
  );
}
```

In `ArtifactDockPanel.tsx`'s `DocumentPane`: read the document, `/agents` and `/members`; compute `author` (agent name by `updatedByAgentId`, else member name by `updatedByUserId`, else "Someone"), `updated` with the existing `timeAgo` helper (`components/teams/shared/lib/timeAgo`), and `rev` from `latestRevisionNumber`; render under the title a line `<p className="text-caption text-muted-foreground">{author} · updated {updated} · rev {rev}</p>`; compute `outline = outlineOf(body)` and render `<DocumentContents>` when `outline.length >= CONTENTS_MIN_HEADINGS`, passing a `bodyRef` that wraps the `DocumentRow`; replace the previous minimal markdown render with `<DocumentRow doc={data} agents={agents} issueId={source.issue} defaultOpen />` so the annotation layer, panel, revision menu, restore and diff come along. Keep the Open task action; an Edit action is added in Task 4.

- [ ] **Step 4: Run the tests, typecheck, lint, scanner, commit**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/workspace/panels/__tests__ && pnpm -F @lightbulb/frontend exec tsc --noEmit -p tsconfig.json && pnpm -F @lightbulb/frontend lint && (cd apps/frontend && node scripts/brand-tokens-scan.mjs)`

```bash
git add apps/frontend/src/components/workspace/panels
git commit -m "documents: the pane shows author, date, version and a contents list, over the reviewable document row"
```

---

### Task 4: Editing on the web

**Files:**
- Modify: `apps/frontend/package.json` (add `"@mdxeditor/editor": "4.3.3"`)
- Create: `apps/frontend/src/components/teams/shared/components/MarkdownEditor.tsx` (lean MDXEditor wrapper, lazy-loaded by its users)
- Create: `apps/frontend/src/components/teams/issues/DocumentEditor.tsx` (title, body, Save / Cancel, conflict handling)
- Modify: `apps/frontend/src/components/teams/issues/DocumentRow.tsx` (an Edit button in its header; `onEdit` opens `DocumentEditor` in place of the body)
- Modify: `apps/frontend/src/components/workspace/panels/ArtifactDockPanel.tsx` (the pane's Edit action calls the row's edit)
- Modify: `apps/frontend/src/app/globals.css` or `apps/frontend/src/components/teams/teams.css` (MDXEditor theme variables mapped to our tokens)
- Test: `apps/frontend/src/components/teams/issues/__tests__/DocumentEditor.test.tsx`

**Interfaces:**
- Consumes: `useTeamsApi().put(path, body)` (check its name: `grep -n "put" apps/frontend/src/hooks/useTeamsApi.ts`), the SWR `mutate` for `/issues/{issue}/documents/{key}`, `/issues/{issue}/documents`, `…/revisions`, `…/annotations…`.
- Produces: `MarkdownEditor({ value, onChange, readOnly?, placeholder? })`; `DocumentEditor({ issueId, basePath?, document, currentUserName, onSaved(document), onCancel })`; `DocumentRow` prop `editable?: boolean` (default true when `issueId` is set) and an internal editing state.

- [ ] **Step 1: Write the failing test (the editor is mocked to a textarea; the save and conflict flows are the subject)**

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

vi.mock("@/components/teams/shared/components/MarkdownEditor", () => ({
  MarkdownEditor: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => <textarea aria-label="Body" value={value} onChange={(e) => onChange(e.target.value)} />,
}));
const put = vi.fn();
const mutate = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ put, read: () => ({ data: undefined, mutate }) }) }));
vi.mock("swr", async (orig) => ({ ...(await orig<typeof import("swr")>()), useSWRConfig: () => ({ mutate }) }));
import { DocumentEditor } from "../DocumentEditor";

const doc = { id: "d1", key: "plan", title: "Launch plan", body: "Old body", latestRevisionId: "r3", latestRevisionNumber: 3 };

it("saves a new revision against the revision it opened", async () => {
  put.mockResolvedValue({ ...doc, body: "New body", latestRevisionId: "r4", latestRevisionNumber: 4 });
  const onSaved = vi.fn();
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={onSaved} onCancel={vi.fn()} />);
  const body = screen.getByRole("textbox", { name: "Body" });
  await userEvent.clear(body);
  await userEvent.type(body, "New body");
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(put).toHaveBeenCalledWith("/issues/i1/documents/plan", { title: "Launch plan", format: "markdown", body: "New body", changeSummary: "Edited by Ada", baseRevisionId: "r3" });
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ latestRevisionId: "r4" })));
});

it("a stale save keeps the draft and names the other editor", async () => {
  put.mockRejectedValue(Object.assign(new Error("conflict"), { status: 409, body: { error: { code: "document_revision_conflict" } } }));
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={vi.fn()} onCancel={vi.fn()} />);
  await userEvent.type(screen.getByRole("textbox", { name: "Body" }), " more");
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/changed since you opened it/i);
  expect(screen.getByRole("textbox", { name: "Body" })).toHaveValue("Old body more");
  expect(screen.getByRole("button", { name: /reload/i })).toBeInTheDocument();
});

it("one save in flight", async () => {
  let release!: () => void;
  put.mockReturnValue(new Promise((r) => { release = () => r({ ...doc, latestRevisionId: "r4" }); }));
  render(<DocumentEditor issueId="i1" document={doc} currentUserName="Ada" onSaved={vi.fn()} onCancel={vi.fn()} />);
  const save = screen.getByRole("button", { name: "Save" });
  await userEvent.click(save);
  await userEvent.click(save);
  expect(put).toHaveBeenCalledTimes(1);
  expect(save).toBeDisabled();
  release();
});
```

- [ ] **Step 2: Run it to see it fail**, then **Step 3: Implement**

`MarkdownEditor.tsx` (the only file that imports `@mdxeditor/editor`):

```tsx
"use client";

import "@mdxeditor/editor/style.css";
import {
  MDXEditor, codeBlockPlugin, codeMirrorPlugin, headingsPlugin, linkDialogPlugin, linkPlugin, listsPlugin,
  markdownShortcutPlugin, quotePlugin, tablePlugin, thematicBreakPlugin, toolbarPlugin,
  BlockTypeSelect, BoldItalicUnderlineToggles, CreateLink, InsertTable, InsertThematicBreak, ListsToggle, UndoRedo,
  type MDXEditorMethods,
} from "@mdxeditor/editor";
import { useRef } from "react";

/** A markdown editor over MDXEditor: headings, lists, quotes, rules, links, tables, code. No image upload, no mentions (later). */
export function MarkdownEditor({ value, onChange, readOnly = false, placeholder }: { value: string; onChange: (markdown: string) => void; readOnly?: boolean; placeholder?: string }) {
  const ref = useRef<MDXEditorMethods>(null);
  return (
    <MDXEditor
      ref={ref}
      className="lb-editor"
      markdown={value}
      onChange={onChange}
      readOnly={readOnly}
      placeholder={placeholder}
      plugins={[
        headingsPlugin(), listsPlugin(), quotePlugin(), thematicBreakPlugin(), linkPlugin(), linkDialogPlugin(), tablePlugin(),
        codeBlockPlugin({ defaultCodeBlockLanguage: "" }),
        codeMirrorPlugin({ codeBlockLanguages: { "": "Plain", js: "JavaScript", ts: "TypeScript", py: "Python", bash: "Shell", json: "JSON", "openui-lang": "Card", mermaid: "Diagram" } }),
        markdownShortcutPlugin(),
        toolbarPlugin({ toolbarContents: () => <><UndoRedo /><BlockTypeSelect /><BoldItalicUnderlineToggles /><ListsToggle /><CreateLink /><InsertTable /><InsertThematicBreak /></> }),
      ]}
    />
  );
}
```

Theme (CSS, tokens only): `.lb-editor { --baseBg: var(--plane-pane); --baseText: var(--fg); --baseBorder: var(--edge); --accentBg: var(--plane-raised); --accentText: var(--link); font-family: inherit; }` — MDXEditor's stylesheet reads these `--base*`/`--accent*` variables; confirm the variable names in `node_modules/@mdxeditor/editor/dist/style.css` and map the ones it uses.

`DocumentEditor.tsx`:

```tsx
"use client";

import { lazy, Suspense, useState } from "react";
import { useSWRConfig } from "swr";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { IssueDocument } from "@/components/teams/shared/types";

const MarkdownEditor = lazy(() => import("@/components/teams/shared/components/MarkdownEditor").then((m) => ({ default: m.MarkdownEditor })));

type Doc = IssueDocument & { latestRevisionId?: string | null };

function isConflict(e: unknown): boolean {
  const err = e as { status?: number; body?: { error?: { code?: string } } };
  return err?.status === 409 || /conflict|locked/i.test(err?.body?.error?.code ?? "");
}

/** Edit a document and save it as a new revision against the one that was opened. */
export function DocumentEditor({ issueId, basePath = `/issues/${issueId}`, document, currentUserName, onSaved, onCancel }: {
  issueId: string; basePath?: string; document: Doc; currentUserName: string; onSaved: (saved: Doc) => void; onCancel: () => void;
}) {
  const { put } = useTeamsApi();
  const { mutate } = useSWRConfig();
  const [title, setTitle] = useState(document.title ?? "");
  const [body, setBody] = useState(document.body ?? "");
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const path = `${basePath}/documents/${document.key}`;

  async function save() {
    if (saving) return;
    setSaving(true); setError(null); setConflict(false);
    try {
      const saved = await put(path, { title: title || null, format: "markdown", body, changeSummary: `Edited by ${currentUserName}`, baseRevisionId: document.latestRevisionId ?? null }) as Doc;
      await Promise.all([mutate(path), mutate(`${basePath}/documents`), mutate((key: unknown) => typeof key === "string" && key.startsWith(`${path}/`))]);
      onSaved(saved);
    } catch (e) {
      if (isConflict(e)) setConflict(true); else setError("Couldn't save. Try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="grid gap-2" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <Input aria-label="Title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title" />
      <Suspense fallback={<textarea aria-label="Body" className="room-input min-h-48" value={body} onChange={(e) => setBody(e.target.value)} />}>
        <MarkdownEditor value={body} onChange={setBody} />
      </Suspense>
      {conflict && <p role="alert" className="text-ui text-err">This document changed since you opened it. Your draft is kept here; <Button type="button" variant="link" onClick={() => mutate(path)}>reload</Button> to see the new version, then copy your changes across.</p>}
      {error && <p role="alert" className="text-ui text-err">{error}</p>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
        <Button type="button" size="sm" variant="outline" disabled={saving} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}
```

Check `useTeamsApi` for the write method's name and error shape (`grep -n "status\|body" apps/frontend/src/hooks/useTeamsApi.ts`): `isConflict` must read the fields the real error carries.

`DocumentRow.tsx`: add an Edit button (pencil icon, `aria-label="Edit document"`) in the header's action area when `reviewEnabled && !historicalPreview`; `editing` state swaps the body block for `<DocumentEditor issueId basePath document={reviewable} currentUserName={…} onSaved={() => { setEditing(false); void mutate(root + "/documents"); }} onCancel={() => setEditing(false)} />`. The current user's name comes from Clerk's `useUser()` (first name or "You"). The pane's Edit action (Task 3) calls the same through a ref or by rendering `DocumentRow` with `startEditing`.

- [ ] **Step 4: Run tests, typecheck, lint, scanner; check the bundle**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/teams/issues/__tests__ src/components/workspace/panels/__tests__ && pnpm -F @lightbulb/frontend exec tsc --noEmit -p tsconfig.json && pnpm -F @lightbulb/frontend lint && (cd apps/frontend && node scripts/brand-tokens-scan.mjs)`
Then confirm `@mdxeditor/editor` is in its own chunk: `cd apps/frontend && pnpm build 2>&1 | grep -i mdx` (or the Next build's route table) — it must not appear in the `/chat` first-load list.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/package.json pnpm-lock.yaml apps/frontend/src/components/teams apps/frontend/src/components/workspace/panels apps/frontend/src/app/globals.css
git commit -m "documents: edit in place with a markdown editor; a save is a new revision, a stale save keeps the draft"
```

---

### Task 5: The phone: header details and a plain editor

**Files:**
- Modify: `apps/mobile/src/lib/teamsData.ts` (add `updateIssueDocument(config, issueId, key, body)` beside the existing mutations, and expose `updateDocument` from `useTeamsIssue`)
- Modify: `apps/mobile/src/app/(app)/teams/issue/[id].tsx` (the documents section: author / updated / rev line; an Edit action opening a sheet)
- Create: `apps/mobile/src/components/teams/DocumentEditSheet.tsx`
- Test: `apps/mobile/src/lib/__tests__/teamsData.documents.test.ts`
- Test: `apps/mobile/src/components/teams/__tests__/DocumentEditSheet.test.tsx`

**Interfaces:**
- Consumes: the phone's Teams adapter request helper (the one `createSubIssue` uses) and `IssueDocument` from `@lightbulb/shared` or the local Teams types.
- Produces: `updateIssueDocument` → `PUT /teams/issues/{issue}/documents/{key}` with the same body as the web; `DocumentEditSheet({ document, onSave(title, body), onClose })` with a conflict alert.

- [ ] **Step 1: Tests first**: `teamsData.documents.test.ts` asserts the helper PUTs the exact body with `baseRevisionId` from `latestRevisionId`; `DocumentEditSheet.test.tsx` renders the sheet, edits the body, presses Save, and asserts `onSave` receives the text; a rejected save with a 409 shows an `Alert` whose message contains "changed since you opened it" (mock `Alert.alert`).

- [ ] **Step 2: Implement** following the existing `createSubIssue` / `createChild` pattern (ref-latched in-flight flag, `Alert.alert` on failure, `load()` after success). The sheet is the existing `BottomSheet` with a title field, a multiline `TextInput` (`textAlignVertical="top"`, min height 240), Save and Cancel. The documents section line reads "`<author>` · updated `<time>` · rev `<n>`" using the agents the screen already has.

- [ ] **Step 3: Run** `pnpm -F @lightbulb/mobile exec jest src/lib/__tests__/teamsData.documents.test.ts src/components/teams/__tests__/DocumentEditSheet.test.tsx && pnpm -F @lightbulb/mobile exec tsc --noEmit && pnpm -F @lightbulb/mobile lint`, drive it once in the simulator (open a task with a document, edit, save, see rev bump), then commit:

```bash
git add apps/mobile
git commit -m "phone: a document shows who wrote it and when, and can be edited in a plain editor"
```

---

### Task 6: Dev acceptance and the PR(s)

- [ ] **Step 1:** Web PR (Tasks 2–4) titled "Documents: cards in documents, a document pane with header and contents, editing with versions"; phone PR (Task 5) or the same PR if the OTA timing suits; the skill (Task 1) per its shipping note.

- [ ] **Step 2: Chrome acceptance on dev**, signed in as a dev test account, after the skill reached the dev runtime:

1. Ask an agent for "a research document on three email providers". The chat gets five lines and a card; the pane opens with the answer first, no "Observed" line, providers in a table, sources listed, and the header shows the agent, "updated just now", rev 1.
2. The document has four or more headings: the contents list is there and scrolls.
3. Select a sentence, Comment, write "cheaper option?"; within a minute the agent replies in the task thread (the rewake), and the thread shows beside the passage.
4. Click Edit, change a line, Save: the header reads rev 2; the version menu shows both, and Restore brings rev 1 back as rev 3. Then open the same document in a second browser session, edit there, and in the first session try to save a stale edit: the conflict notice appears and the draft stays.
5. Ask the agent to "update the research doc with a fourth provider": it writes the same document (rev bumps, a change summary appears), not a "v2".
6. Paste a card fence into a document yourself: it renders as a disabled card.
7. On the phone (simulator): open the task, see the author line, edit and save.

Paste the observations and screenshots on the PR.
