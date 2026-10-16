# File Artifacts in the Pane (Track B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A file an agent made (a spreadsheet, an HTML page, a PDF, a text or markdown file) shows as a card in chat and opens in the right pane in a viewer for its type; a mermaid fence draws inline; the Library and document cards open the same pane instead of a modal.

**Architecture:** One new pane kind, `artifact`, resolves a `src` (a room attachment, a Paperclip artifact, or a workspace file) to bytes through the routes that already serve them, and renders through `components/media/FileViewerContent.tsx`, which is already the one shared "how do I show this type" component (images, video, PDF today). This plan adds spreadsheet (SheetJS, parsed in the browser), sandboxed HTML, markdown and text branches to it, routes document-like room attachments to the pane while images and video keep their lightbox, adds `text/html` as an attachment type end to end, and deletes `ArtifactViewerDialog`. Mermaid is a lazy `code` override beside the card one from Track A.

**Tech Stack:** SheetJS CE `xlsx` 0.20.3 from the SheetJS CDN tarball (the npm registry copy is stale at 0.18.5 with known CVEs), mermaid 11 (already installed), react-markdown 10, vitest + Testing Library (web), pytest (backend, bridge).

**Spec:** `docs/superpowers/specs/2026-10-16-generative-ui-and-artifacts-design.md`, sections B1–B3 and the "B. File artifacts" mechanisms; the document view in the pane is the minimal C2 so that the dialog can be deleted (Track C's plan adds the header, contents list, comments and editor).

**Depends on:** Track A's PR (the `code` override in `roomMarkdown.tsx`, `CardContext`, the lazy `OpenUICard`) merged to `main`. Branch from `origin/main` after it lands.

## Global Constraints

- Everything a viewer loads comes through an authenticated route into a `Blob` or text in the page: `GET /messaging/rooms/{room}/attachments/{id}` (room), the Teams BFF `issueAttachmentContentPath` / `GET /teams/issues/{issue}/documents/{key}` (Paperclip), the workspace file read (`FileViewer`'s existing fetch). Never a bare `<a href>`/`<img src>` to an API path.
- HTML never runs on our origin: the API serves `text/html` attachments with `Content-Disposition: attachment` and `Content-Security-Policy: sandbox`; the viewer renders fetched text in `<iframe sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc=…>` — never `allow-same-origin`, never a `src` pointing at our API. Scripts are allowed (owner decision 2026-10-16). The frontend's own CSP is report-only today with `'unsafe-inline'` in `script-src`; a `srcdoc` frame inherits it, so if that CSP is ever enforced, inline scripts in previews keep working and external `<script src>` follow its allow-list.
- Spreadsheet parsing is in the browser only (SheetJS from the blob), never on the backend. Show the first 2,000 rows per sheet, one tab per sheet, with a line saying so when truncated. View only.
- Viewer caps: HTML 2 MiB, spreadsheet/CSV 5 MiB (the backend text/office caps already bound uploads; the viewer shows a "too large to preview, download instead" card past its own cap).
- `text/html` joins the attachment type tables in lockstep: backend `_ATTACHMENT_SIGNATURES` + `ATTACHMENT_MAX_BYTES_BY_TYPE` (`core/services/messaging.py`), the shared `MESSAGE_ATTACHMENT_CONTENT_TYPES` union, the bridge's `MEDIA_CONTENT_TYPES` / `ATTACHMENT_TYPES` / `_NON_IMAGE_ATTACHMENT_TYPES` (`apps/infra/hermes/bridge/turn.py`). All or none.
- Images and video keep the existing lightbox (`FileViewerDialog`, prev/next); document-like kinds (`application/pdf`, xlsx, `text/csv`, `text/html`, `text/markdown`, `text/plain`) open the pane; `application/zip`, docx and pptx stay download-only chips (no viewer exists for them).
- No colour literals outside the scanner's exemptions. Mermaid's theme needs real colours, so they are read at render time from the CSS variables (`getComputedStyle(document.documentElement).getPropertyValue("--fg")` etc.), never written as literals in source.
- Pane kinds are registered in three places plus the component map: `PanelKind`, `describePanel`, `panelUrlParamKeys`, `clearablePanelParamKeys`, `opensInPane` in `panelRegistry.ts`, and `PANEL_COMPONENTS` in `panelComponents.ts`. A pane must restore from its URL alone.
- Branch each PR from `origin/main` in its own worktree under `lightbulb.nosync/.claude/worktrees/`. Each PR: CI green and Codex reviewed before merge. Dev acceptance in Chrome with receipts on the PR. Phone flows in the simulator.
- Every commit message ends with a blank line and then exactly `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. **A `text/html` page that probes its sandbox.** A preview page that reads `document.cookie`, `localStorage`, calls `fetch` to the API with credentials, or calls `window.top.location` must get nothing: empty cookie, a SecurityError on storage, a CORS/credential failure, and no navigation (Task 2 test "the frame has no origin"; Task 7 acceptance step with a probe page).
2. **A spreadsheet with 50,000 rows or 40 sheets.** The grid must stay responsive: 2,000 rows per sheet, tabs for sheets, a truncation line, no freeze (Task 2 test "rows are bounded").
3. **An attachment whose declared type lies.** A `.csv` that is really binary, or a `.html` that is 10 MB: the backend already refuses non-UTF-8 text and over-cap bytes; the viewer must show the "could not preview" card, not a blank pane or a hung parser (Task 2 test "unparseable bytes show the fallback card").
4. **A pane URL copied to another person.** `?panel=artifact&src=attachment&room=…&id=…` opened by someone who is not a member must show "You can't open this file", not a spinner forever (Task 3 test "a refused fetch shows the error card").
5. **A mermaid fence that is not mermaid.** Garbage after ```` ```mermaid ```` must render the source as code with a one-line note, never throw, and never inject unsanitised HTML (`securityLevel: "strict"`) (Task 5 test "a bad diagram falls back to code").

---

### Task 1: `text/html` as an attachment type, end to end

**Files:**
- Modify: `apps/backend/core/services/messaging.py` (`_ATTACHMENT_SIGNATURES` ~line 1240, `ATTACHMENT_MAX_BYTES_BY_TYPE` ~line 77, the error text in `validate_attachment_bytes` ~line 1290)
- Modify: `apps/backend/routers/messaging.py` (`get_room_attachment`, ~line 1032: the `Content-Disposition` expression and a CSP header)
- Modify: `packages/shared/src/messaging/types.ts` (`MESSAGE_ATTACHMENT_CONTENT_TYPES`)
- Modify: `apps/infra/hermes/bridge/turn.py` (`MEDIA_CONTENT_TYPES` ~line 1089, `ATTACHMENT_TYPES` and `_NON_IMAGE_ATTACHMENT_TYPES` ~line 1009)
- Modify: `apps/mobile/src/lib/attachmentFile.ts` and `apps/mobile/src/components/room/AttachmentViewerModal.tsx` only if `tsc` reports an exhaustive `Record<MessageAttachmentContentType, …>` missing the new key
- Test: `apps/backend/tests/unit/services/test_messaging_service_review_findings.py` (the text-type parametrize at ~line 441)
- Test: `apps/backend/tests/unit/routers/test_messaging_attachment_headers.py` (create)
- Test: `apps/infra/hermes/tests/test_attachments.py`

**Interfaces:**
- Produces: `"text/html"` accepted by `validate_attachment_bytes` (UTF-8, `TEXT_MAX_BYTES`), served with `Content-Disposition: attachment; filename*=…` and `Content-Security-Policy: sandbox`; `MessageAttachmentContentType` includes `"text/html"`; the bridge maps `.html` and `.htm` to it.

- [ ] **Step 1: Backend tests first**

In `test_messaging_service_review_findings.py`, extend the parametrize at ~line 441 to `[("text/csv", ".csv"), ("text/plain", ".txt"), ("text/markdown", ".md"), ("text/html", ".html")]`.

Create `apps/backend/tests/unit/routers/test_messaging_attachment_headers.py`:

```python
"""A text/html attachment never renders on the API origin: it downloads, and carries a sandbox CSP."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext, get_current_user
from routers import messaging as messaging_router


def _app(content_type: str, file_name: str) -> FastAPI:
    app = FastAPI()
    app.include_router(messaging_router.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_1", org_id="org_1", email="u@example.com", claims={})
    meta = ({"ownerId": "org_1"}, None)
    attachment = SimpleNamespace(content_type=content_type, file_name=file_name)
    patch.object(messaging_router.service, "require_room_member", AsyncMock(return_value=meta)).start()
    patch.object(messaging_router, "run_in_thread", AsyncMock(return_value=(attachment, b"<html>hi</html>"))).start()
    patch.object(messaging_router, "resolve_owner_id", lambda auth: "org_1").start()
    return app


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type,file_name,disposition,csp", [
    ("text/html", "page.html", "attachment", "sandbox"),
    ("application/zip", "bundle.zip", "attachment", None),
    ("application/pdf", "report.pdf", "inline", None),
])
async def test_html_downloads_with_a_sandbox_csp(content_type, file_name, disposition, csp):
    async with AsyncClient(transport=ASGITransport(app=_app(content_type, file_name)), base_url="http://t") as client:
        response = await client.get("/api/v1/messaging/rooms/room-1/attachments/att-1")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith(f"{disposition}; filename*=UTF-8''{file_name}")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers.get("content-security-policy") == csp
```

If `AuthContext`'s constructor takes different fields, copy the construction an existing router test uses (`grep -rn "AuthContext(" apps/backend/tests/unit/routers | head -3`).

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_service_review_findings.py -k "text/html or html" tests/unit/routers/test_messaging_attachment_headers.py -v`
Expected: FAIL (`text/html` not in the signature table; no CSP header).

- [ ] **Step 3: Backend changes**

In `core/services/messaging.py`:
- `ATTACHMENT_MAX_BYTES_BY_TYPE`: add `"text/html": TEXT_MAX_BYTES,` after `"text/markdown"`.
- `_ATTACHMENT_SIGNATURES`: add `"text/html": (None, ".html"),` after `"text/markdown"`.
- The `ValueError` text in `validate_attachment_bytes`: add `HTML` to the list ("…CSV, TXT, MD, HTML, MP4…").

In `routers/messaging.py` `get_room_attachment`, replace the `Content-Disposition` line and add the CSP:

```python
    # ZIPs are opaque downloads; HTML must never render on this origin (a page an agent
    # wrote, or was tricked into writing, would run as the API). Both download; the
    # viewer renders HTML in a sandboxed frame from the fetched text instead.
    download_only = attachment.content_type in ("application/zip", "text/html")
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"{'attachment' if download_only else 'inline'}; filename*=UTF-8''{quote(attachment.file_name, safe='')}",
    }
    if attachment.content_type == "text/html":
        headers["Content-Security-Policy"] = "sandbox"
    return Response(content=data, media_type=attachment.content_type, headers=headers)
```

- [ ] **Step 4: Shared and bridge**

`packages/shared/src/messaging/types.ts`: add `"text/html",` after `"text/markdown",` in `MESSAGE_ATTACHMENT_CONTENT_TYPES` with the comment `// HTML -- served as a download, rendered only in a sandboxed frame`.

`apps/infra/hermes/bridge/turn.py`: in `MEDIA_CONTENT_TYPES` add `".html": "text/html", ".htm": "text/html",`; in `ATTACHMENT_TYPES` add `"text/html"` after `"text/markdown"`; in `_NON_IMAGE_ATTACHMENT_TYPES` add `"text/html"`.

In `apps/infra/hermes/tests/test_attachments.py`, find the test that enumerates text media types (grep `text/markdown`) and add the `.html` → `text/html` case to it in the same shape.

- [ ] **Step 5: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_messaging_service_review_findings.py tests/unit/routers/test_messaging_attachment_headers.py -v && uv run pytest ../infra/hermes/tests/test_attachments.py -v`
Expected: PASS.

Run: `pnpm -F @lightbulb/shared typecheck && pnpm -F @lightbulb/frontend exec tsc --noEmit -p tsconfig.json && pnpm -F @lightbulb/mobile exec tsc --noEmit`
Expected: clean (three pre-existing `IssueDetailPage.*.test.tsx` errors on web only). If the phone reports an exhaustive map missing `text/html`, add the entry (label "HTML", a `FileText`-style icon) where its siblings are.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/messaging.py apps/backend/routers/messaging.py apps/backend/tests/unit/services/test_messaging_service_review_findings.py apps/backend/tests/unit/routers/test_messaging_attachment_headers.py packages/shared/src/messaging/types.ts apps/infra/hermes/bridge/turn.py apps/infra/hermes/tests/test_attachments.py apps/mobile
git commit -m "attachments: text/html is a room attachment that downloads and never renders on the API origin"
```

---

### Task 2: Spreadsheet, HTML, markdown and text branches in the shared file viewer

**Files:**
- Modify: `apps/frontend/package.json` (add `"xlsx": "https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz"`)
- Modify: `apps/frontend/src/components/media/FileViewerContent.tsx` (`FileViewerItem` gains `blob?: Blob`; new branches)
- Create: `apps/frontend/src/components/media/SheetGrid.tsx`
- Create: `apps/frontend/src/components/media/HtmlPreview.tsx`
- Create: `apps/frontend/src/components/media/TextPreview.tsx`
- Test: `apps/frontend/src/components/media/__tests__/FileViewerContent.types.test.tsx`

**Interfaces:**
- Produces: `FileViewerItem { fileName; contentType; size?; url; poster?; blob? }`; `FileViewerContent` renders `xlsx`/`text/csv` as `SheetGrid`, `text/html` as `HtmlPreview`, `text/markdown` and `text/plain` as `TextPreview`, each needing `item.blob` (without it, the existing fallback card). `isPaneViewable(contentType): boolean` exported from `FileViewerContent.tsx` (true for pdf, xlsx, csv, html, markdown, plain).

- [ ] **Step 1: Add SheetJS**

In `apps/frontend/package.json` dependencies add `"xlsx": "https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz",` and run `pnpm install`. Confirm `pnpm why xlsx` shows 0.20.3 under the frontend only and the lockfile records an `integrity` for the tarball.

- [ ] **Step 2: Write the failing tests**

`apps/frontend/src/components/media/__tests__/FileViewerContent.types.test.tsx`:

```tsx
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { FileViewerContent, isPaneViewable } from "../FileViewerContent";

const XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const blob = (text: string, type: string) => new Blob([text], { type });

it("isPaneViewable names the document-like kinds only", () => {
  expect(["application/pdf", XLSX_TYPE, "text/csv", "text/html", "text/markdown", "text/plain"].every(isPaneViewable)).toBe(true);
  expect(["image/png", "video/mp4", "application/zip", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"].some(isPaneViewable)).toBe(false);
});

it("renders a CSV as a grid with a header row", async () => {
  render(<FileViewerContent item={{ fileName: "vendors.csv", contentType: "text/csv", url: "blob:x", blob: blob("Vendor,Price\nAcme,120\nBirch,99\n", "text/csv") }} />);
  const grid = await screen.findByRole("table");
  expect(within(grid).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Vendor", "Price"]);
  expect(within(grid).getAllByRole("row")).toHaveLength(3);
});

it("rows are bounded", async () => {
  const big = "n\n" + Array.from({ length: 5000 }, (_, i) => String(i)).join("\n");
  render(<FileViewerContent item={{ fileName: "big.csv", contentType: "text/csv", url: "blob:x", blob: blob(big, "text/csv") }} />);
  const grid = await screen.findByRole("table");
  expect(within(grid).getAllByRole("row")).toHaveLength(2001);
  expect(screen.getByText(/showing the first 2,000 of 5,000 rows/i)).toBeInTheDocument();
});

it("unparseable bytes show the fallback card", async () => {
  render(<FileViewerContent item={{ fileName: "x.xlsx", contentType: XLSX_TYPE, url: "blob:x", blob: new Blob([new Uint8Array([0, 1, 2, 3])], { type: XLSX_TYPE }) }} />);
  expect(await screen.findByText(/could not preview/i)).toBeInTheDocument();
});

it("the frame has no origin", async () => {
  render(<FileViewerContent item={{ fileName: "page.html", contentType: "text/html", url: "blob:x", blob: blob("<p>hi</p>", "text/html") }} />);
  const frame = await screen.findByTitle("page.html");
  expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
  expect(frame.getAttribute("src")).toBeNull();
  expect(frame.getAttribute("srcdoc")).toContain("<p>hi</p>");
  expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
});

it("markdown renders and plain text stays monospace", async () => {
  render(<FileViewerContent item={{ fileName: "notes.md", contentType: "text/markdown", url: "blob:x", blob: blob("# Title\n\nbody", "text/markdown") }} />);
  expect(await screen.findByRole("heading", { name: "Title" })).toBeInTheDocument();
  render(<FileViewerContent item={{ fileName: "log.txt", contentType: "text/plain", url: "blob:y", blob: blob("line 1\nline 2", "text/plain") }} />);
  expect(await screen.findByText(/line 1/)).toBeInTheDocument();
});

it("a two-sheet workbook shows tabs", async () => {
  const { utils, write } = await import("xlsx");
  const wb = utils.book_new();
  utils.book_append_sheet(wb, utils.aoa_to_sheet([["a"], [1]]), "First");
  utils.book_append_sheet(wb, utils.aoa_to_sheet([["b"], [2]]), "Second");
  const bytes = write(wb, { type: "array", bookType: "xlsx" }) as ArrayBuffer;
  render(<FileViewerContent item={{ fileName: "two.xlsx", contentType: XLSX_TYPE, url: "blob:x", blob: new Blob([bytes], { type: XLSX_TYPE }) }} />);
  await userEvent.click(await screen.findByRole("tab", { name: "Second" }));
  expect(within(screen.getByRole("table")).getAllByRole("columnheader")[0]).toHaveTextContent("b");
});
```

- [ ] **Step 3: Run them to see them fail**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/media/__tests__/FileViewerContent.types.test.tsx`
Expected: FAIL (`isPaneViewable` not exported; CSV renders the fallback card).

- [ ] **Step 4: The grid**

`apps/frontend/src/components/media/SheetGrid.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export const SHEET_MAX_ROWS = 2_000;
export const SHEET_MAX_BYTES = 5 * 1024 * 1024;

type Sheet = { name: string; header: string[]; rows: string[][]; total: number };
type State = { status: "loading" } | { status: "ready"; sheets: Sheet[] } | { status: "error" };

const text = (v: unknown) => (v === null || v === undefined ? "" : String(v));

/** A workbook or CSV parsed in the browser with SheetJS: one tab per sheet, the first 2,000 rows each. View only. */
export function SheetGrid({ blob, fileName }: { blob: Blob; fileName: string }) {
  const [state, setState] = useState<State>({ status: "loading" });
  const [active, setActive] = useState(0);
  useEffect(() => {
    let live = true;
    (async () => {
      try {
        if (blob.size > SHEET_MAX_BYTES) throw new Error("too large");
        const { read, utils } = await import("xlsx");
        const wb = read(await blob.arrayBuffer(), { type: "array", dense: true });
        const sheets = wb.SheetNames.map((name) => {
          const aoa = utils.sheet_to_json<unknown[]>(wb.Sheets[name], { header: 1, blankrows: false }) as unknown[][];
          const [head = [], ...body] = aoa;
          return { name, header: head.map(text), rows: body.slice(0, SHEET_MAX_ROWS).map((r) => r.map(text)), total: body.length };
        });
        if (!sheets.length) throw new Error("empty");
        if (live) setState({ status: "ready", sheets });
      } catch {
        if (live) setState({ status: "error" });
      }
    })();
    return () => { live = false; };
  }, [blob]);
  if (state.status === "loading") return <p className="p-4 text-ui text-muted-foreground">Loading {fileName}…</p>;
  if (state.status === "error") return <p role="alert" className="p-4 text-ui text-muted-foreground">Could not preview {fileName}. Download it instead.</p>;
  const sheet = state.sheets[Math.min(active, state.sheets.length - 1)];
  const width = Math.max(sheet.header.length, ...sheet.rows.map((r) => r.length));
  return (
    <div className="flex h-full min-h-0 flex-col">
      {state.sheets.length > 1 && (
        <Tabs value={String(active)} onValueChange={(v) => setActive(Number(v))}>
          <TabsList>{state.sheets.map((s, i) => <TabsTrigger key={s.name} value={String(i)}>{s.name}</TabsTrigger>)}</TabsList>
        </Tabs>
      )}
      <div className="min-h-0 flex-1 overflow-auto">
        <Table>
          <TableHeader><TableRow>{Array.from({ length: width }, (_, c) => <TableHead key={c}>{sheet.header[c] ?? ""}</TableHead>)}</TableRow></TableHeader>
          <TableBody>{sheet.rows.map((r, i) => <TableRow key={i}>{Array.from({ length: width }, (_, c) => <TableCell key={c}>{r[c] ?? ""}</TableCell>)}</TableRow>)}</TableBody>
        </Table>
      </div>
      {sheet.total > SHEET_MAX_ROWS && <p className="p-2 text-caption text-muted-foreground">Showing the first {SHEET_MAX_ROWS.toLocaleString()} of {sheet.total.toLocaleString()} rows.</p>}
    </div>
  );
}
```

If `components/ui/tabs.tsx` exports differ (`Tabs`, `TabsList`, `TabsTrigger` are the shadcn names), use what it exports.

- [ ] **Step 5: HTML and text**

`apps/frontend/src/components/media/HtmlPreview.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";

export const HTML_MAX_BYTES = 2 * 1024 * 1024;

/**
 * A page an agent wrote, shown in a frame with no origin of ours: scripts may run (owner
 * decision), nothing else is allowed — no same-origin, no forms, no pop-ups, no navigation.
 * The text comes from the authenticated fetch; the frame never loads an API URL.
 */
export function HtmlPreview({ blob, fileName }: { blob: Blob; fileName: string }) {
  const [html, setHtml] = useState<string | null | "too-large">(null);
  useEffect(() => {
    let live = true;
    if (blob.size > HTML_MAX_BYTES) { setHtml("too-large"); return; }
    void blob.text().then((t) => { if (live) setHtml(t); });
    return () => { live = false; };
  }, [blob]);
  if (html === null) return <p className="p-4 text-ui text-muted-foreground">Loading {fileName}…</p>;
  if (html === "too-large") return <p role="alert" className="p-4 text-ui text-muted-foreground">{fileName} is too large to preview. Download it instead.</p>;
  return <iframe title={fileName} sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={html} className="h-full w-full border-0" style={{ background: "var(--plane-pane)" }} />;
}
```

`apps/frontend/src/components/media/TextPreview.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";

export const TEXT_MAX_BYTES = 1024 * 1024;

export function TextPreview({ blob, fileName, markdown }: { blob: Blob; fileName: string; markdown: boolean }) {
  const [text, setText] = useState<string | null | "too-large">(null);
  useEffect(() => {
    let live = true;
    if (blob.size > TEXT_MAX_BYTES) { setText("too-large"); return; }
    void blob.text().then((t) => { if (live) setText(t); });
    return () => { live = false; };
  }, [blob]);
  if (text === null) return <p className="p-4 text-ui text-muted-foreground">Loading {fileName}…</p>;
  if (text === "too-large") return <p role="alert" className="p-4 text-ui text-muted-foreground">{fileName} is too large to preview. Download it instead.</p>;
  if (markdown) return <div className="p-4"><MarkdownView skipHtml>{text}</MarkdownView></div>;
  return <pre className="h-full overflow-auto p-4 text-caption font-mono whitespace-pre-wrap" style={{ background: "var(--plane-pane)" }}>{text}</pre>;
}
```

- [ ] **Step 6: Wire the branches**

In `FileViewerContent.tsx`: add `blob?: Blob;` to `FileViewerItem` (doc: "The bytes, when the caller has them; the spreadsheet, HTML and text branches need them."), add

```tsx
const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const PANE_VIEWABLE = new Set(["application/pdf", XLSX, "text/csv", "text/html", "text/markdown", "text/plain"]);
/** Kinds that open in the right pane (documents). Images and video keep the lightbox; zip/docx/pptx have no viewer. */
export function isPaneViewable(contentType: string): boolean { return PANE_VIEWABLE.has(contentType); }
```

and, before the final fallback `return`, the branches:

```tsx
  if (item.blob && (item.contentType === XLSX || item.contentType === "text/csv")) return <SheetGrid blob={item.blob} fileName={item.fileName} />;
  if (item.blob && item.contentType === "text/html") return <HtmlPreview blob={item.blob} fileName={item.fileName} />;
  if (item.blob && (item.contentType === "text/markdown" || item.contentType === "text/plain")) return <TextPreview blob={item.blob} fileName={item.fileName} markdown={item.contentType === "text/markdown"} />;
```

with the three imports. Replace the fallback card's `text-midnight` / `text-midnight-muted` classes with `text-ui` / `text-muted-foreground` only if the brand scanner complains (they are legacy aliases, not literals).

- [ ] **Step 7: Run the tests, typecheck, lint, scanner**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/media/__tests__ && pnpm -F @lightbulb/frontend exec tsc --noEmit -p tsconfig.json && pnpm -F @lightbulb/frontend lint && (cd apps/frontend && node scripts/brand-tokens-scan.mjs)`
Expected: PASS (7 tests), typecheck clean but for the three known errors, lint 0 errors, scanner 0 literals.

- [ ] **Step 8: Commit**

```bash
git add apps/frontend/package.json pnpm-lock.yaml apps/frontend/src/components/media
git commit -m "viewer: spreadsheets as a grid, HTML in a sandboxed frame, markdown and text inline"
```

---

### Task 3: The `artifact` pane

**Files:**
- Modify: `apps/frontend/src/components/workspace/panelRegistry.ts` (`PanelKind`, `describePanel`, `panelUrlParamKeys`, `clearablePanelParamKeys`, `opensInPane`)
- Modify: `apps/frontend/src/components/workspace/panelComponents.ts`
- Create: `apps/frontend/src/components/workspace/panels/ArtifactDockPanel.tsx`
- Create: `apps/frontend/src/components/workspace/panels/artifactSource.ts`
- Test: `apps/frontend/src/components/workspace/panels/__tests__/artifactSource.test.ts`
- Test: `apps/frontend/src/components/workspace/panels/__tests__/ArtifactDockPanel.test.tsx`

**Interfaces:**
- Consumes: `FileViewerContent`, `isPaneViewable` (Task 2); `useApi()` (`lib/api.ts`: `get`, and the room attachment download the room already uses); `useTeamsApi()` (`read`, `download`); `MarkdownView` with `roomMarkdown` (Track A) for document bodies.
- Produces: pane kind `artifact` opened as `openPanel("artifact", params)` with params one of:
  - `{ src: "attachment", room, id, name?, type? }` (a room attachment),
  - `{ src: "paperclip", kind: "document", issue, key, ident? }` (a Paperclip issue document),
  - `{ src: "paperclip", kind: "attachment" | "work_product", issue, id, name?, type? }` (an issue attachment or a file-backed work product),
  - `{ src: "file", agent, path }` (a workspace file — delegates to the existing `FileViewer` by opening the `file-viewer` pane; included so one key covers every file link).
  `parseArtifactSource(params): ArtifactSource | null` (pure).

- [ ] **Step 1: The pure source parser and its test**

`artifactSource.ts`:

```ts
export type ArtifactSource =
  | { src: "attachment"; room: string; id: string; name: string | null; type: string | null }
  | { src: "paperclip"; kind: "document"; issue: string; key: string; ident: string | null }
  | { src: "paperclip"; kind: "attachment" | "work_product"; issue: string; id: string; name: string | null; type: string | null }
  | { src: "file"; agent: string; path: string };

/** The pane's URL params → a typed source, or null when a required part is missing. */
export function parseArtifactSource(p: Record<string, string>): ArtifactSource | null {
  if (p.src === "attachment" && p.room && p.id) return { src: "attachment", room: p.room, id: p.id, name: p.name ?? null, type: p.type ?? null };
  if (p.src === "paperclip" && p.kind === "document" && p.issue && p.key) return { src: "paperclip", kind: "document", issue: p.issue, key: p.key, ident: p.ident ?? null };
  if (p.src === "paperclip" && (p.kind === "attachment" || p.kind === "work_product") && p.issue && p.id) return { src: "paperclip", kind: p.kind, issue: p.issue, id: p.id, name: p.name ?? null, type: p.type ?? null };
  if (p.src === "file" && p.agent && p.path) return { src: "file", agent: p.agent, path: p.path };
  return null;
}

export const ARTIFACT_PARAM_KEYS = ["src", "room", "id", "kind", "issue", "key", "ident", "name", "type", "agent", "path"] as const;
```

`__tests__/artifactSource.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { parseArtifactSource } from "../artifactSource";

describe("parseArtifactSource", () => {
  it("accepts each source shape and rejects a missing part", () => {
    expect(parseArtifactSource({ src: "attachment", room: "r", id: "a" })).toEqual({ src: "attachment", room: "r", id: "a", name: null, type: null });
    expect(parseArtifactSource({ src: "paperclip", kind: "document", issue: "i", key: "plan" })).toMatchObject({ kind: "document", key: "plan" });
    expect(parseArtifactSource({ src: "paperclip", kind: "work_product", issue: "i", id: "w", type: "application/pdf" })).toMatchObject({ kind: "work_product", type: "application/pdf" });
    expect(parseArtifactSource({ src: "file", agent: "ag", path: "work/out.csv" })).toMatchObject({ src: "file" });
    expect(parseArtifactSource({ src: "attachment", room: "r" })).toBeNull();
    expect(parseArtifactSource({ src: "nope" })).toBeNull();
  });
});
```

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/workspace/panels/__tests__/artifactSource.test.ts` → PASS.

- [ ] **Step 2: Register the pane kind**

`panelRegistry.ts`:
- `PanelKind` union: add `"artifact"`.
- In `describePanel`, next to the `file-viewer` branch:

```ts
  if (key === "artifact") {
    const p: Record<string, string> = {};
    for (const k of ARTIFACT_PARAM_KEYS) if (params[k]) p[k] = params[k];
    if (!parseArtifactSource(p)) return null;
    return { panelId: "artifact", component: "artifact", title: p.name ?? p.ident ?? "File", params: p };
  }
```
  (import both from `./panels/artifactSource`).
- `panelUrlParamKeys`: `if (panelId === "artifact") return ["panel", ...ARTIFACT_PARAM_KEYS];`
- `clearablePanelParamKeys`: `if (panelId === "artifact") return [...ARTIFACT_PARAM_KEYS];`
- `opensInPane`: add `|| descriptor.component === "artifact"`.

`panelComponents.ts`: import `ArtifactDockPanel` and add `artifact: ArtifactDockPanel,`.

- [ ] **Step 3: The pane component test**

`__tests__/ArtifactDockPanel.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

const download = vi.fn();
const teamsRead = vi.fn();
const teamsDownload = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn(), download }) }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: teamsRead, download: teamsDownload }) }));
vi.mock("@/components/workspace/workspaceController", () => ({ closePane: vi.fn(), openPanel: vi.fn() }));
import { ArtifactDockPanel } from "../ArtifactDockPanel";

it("opens a room CSV attachment in the grid", async () => {
  download.mockResolvedValue(new Blob(["a,b\n1,2\n"], { type: "text/csv" }));
  render(<ArtifactDockPanel id="artifact" params={{ src: "attachment", room: "r1", id: "att1", name: "out.csv", type: "text/csv" }} />);
  expect(await screen.findByRole("table")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "out.csv" })).toBeInTheDocument();
  expect(download).toHaveBeenCalledWith("r1", "att1");
});

it("a refused fetch shows the error card", async () => {
  download.mockRejectedValue(Object.assign(new Error("forbidden"), { status: 403 }));
  render(<ArtifactDockPanel id="artifact" params={{ src: "attachment", room: "r1", id: "att1", name: "out.csv", type: "text/csv" }} />);
  expect(await screen.findByRole("alert")).toHaveTextContent(/can't open this file/i);
});

it("renders a Paperclip document body as markdown with an Open task action", async () => {
  teamsRead.mockReturnValue({ data: { id: "d", key: "plan", title: "Launch plan", body: "# Launch\n\nStep one.", format: "markdown" } });
  render(<ArtifactDockPanel id="artifact" params={{ src: "paperclip", kind: "document", issue: "i1", key: "plan", ident: "ISOA-9" }} />);
  expect(await screen.findByRole("heading", { name: "Launch plan" })).toBeInTheDocument();
  expect(screen.getByText("Step one.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /open task/i })).toBeInTheDocument();
});
```

Check how `useApi()` exposes the room attachment download in `MessagingWorkspaceContext.tsx` (it passes `onDownloadAttachment` into `RoomPanel`); mirror the same call the room makes. If the download lives on the messaging client rather than `useApi`, mock that module instead and say so in the report.

- [ ] **Step 4: The pane**

`ArtifactDockPanel.tsx`:

```tsx
"use client";

import { useEffect, useMemo, useState } from "react";
import { Download, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FileViewerContent, isPaneViewable } from "@/components/media/FileViewerContent";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown } from "@/components/messaging/roomMarkdown";
import { CardContext } from "@/components/messaging/openui/CardContext";
import { issueAttachmentContentPath } from "@/components/teams/shared/lib/attachments";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useApi } from "@/lib/api";
import { closePane, openPanel } from "../workspaceController";
import type { PanelProps } from "../panelComponents";
import { parseArtifactSource, type ArtifactSource } from "./artifactSource";

type Loaded = { status: "loading" } | { status: "ready"; blob: Blob; url: string; name: string; type: string } | { status: "error"; message: string };

function describeError(e: unknown): string {
  const status = (e as { status?: number })?.status;
  return status === 403 || status === 404 ? "You can't open this file." : "Could not load this file.";
}

/** The right pane for a file: a room attachment, a Paperclip artifact, or a workspace file. */
export function ArtifactDockPanel({ params }: PanelProps) {
  const source = useMemo(() => parseArtifactSource(params), [params]);
  if (!source) return <PaneFrame title="File"><p role="alert" className="p-4 text-ui">This link does not point at a file.</p></PaneFrame>;
  if (source.src === "file") { openPanel("file-viewer", { agent: source.agent, path: source.path }); return null; }
  if (source.src === "paperclip" && source.kind === "document") return <DocumentPane source={source} />;
  return <FilePane source={source} />;
}

function PaneFrame({ title, actions, children }: { title: string; actions?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="flex h-full min-h-0 flex-col">
      <header className="flex items-center gap-2 border-b px-3 py-2" style={{ borderColor: "var(--edge)" }}>
        <h2 className="min-w-0 flex-1 truncate text-ui font-semibold">{title}</h2>
        {actions}
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Close" onClick={closePane}><X aria-hidden="true" /></Button>
      </header>
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </section>
  );
}

function FilePane({ source }: { source: Exclude<ArtifactSource, { src: "file" } | { kind: "document" }> }) {
  const api = useApi();
  const teams = useTeamsApi();
  const [state, setState] = useState<Loaded>({ status: "loading" });
  useEffect(() => {
    let live = true;
    let url: string | undefined;
    const fetchBlob = source.src === "attachment"
      ? api.download(source.room, source.id)
      : teams.download(issueAttachmentContentPath(source.issue, source.id));
    fetchBlob.then((blob) => {
      if (!live) return;
      url = URL.createObjectURL(blob);
      setState({ status: "ready", blob, url, name: source.name ?? "file", type: source.type ?? blob.type ?? "application/octet-stream" });
    }).catch((e) => { if (live) setState({ status: "error", message: describeError(e) }); });
    return () => { live = false; if (url) URL.revokeObjectURL(url); };
  }, [source, api, teams]);
  const title = source.name ?? "File";
  const download = state.status === "ready" ? <a className="inline-flex" href={state.url} download={state.name} aria-label="Download"><Download aria-hidden="true" /></a> : null;
  return (
    <PaneFrame title={title} actions={download}>
      {state.status === "loading" && <p className="p-4 text-ui text-muted-foreground">Loading…</p>}
      {state.status === "error" && <p role="alert" className="p-4 text-ui">{state.message}</p>}
      {state.status === "ready" && (isPaneViewable(state.type) || state.type.startsWith("image/") || state.type.startsWith("video/")
        ? <FileViewerContent item={{ fileName: state.name, contentType: state.type, size: state.blob.size, url: state.url, blob: state.blob }} className="h-full" />
        : <p className="p-4 text-ui text-muted-foreground">No preview for this file type. Use Download.</p>)}
    </PaneFrame>
  );
}

/** The minimal document view (Track C adds the header details, contents list, comments and editing). */
function DocumentPane({ source }: { source: Extract<ArtifactSource, { kind: "document" }> }) {
  const { read } = useTeamsApi();
  const { data, error } = read<{ id: string; key: string; title?: string | null; body?: string | null }>(`/issues/${source.issue}/documents/${source.key}`);
  const title = data?.title?.trim() || source.key;
  const openTask = <Button type="button" variant="outline" size="sm" onClick={() => openPanel("teams-issue-detail", { detailId: source.issue }, { pushUrl: true })}>Open task</Button>;
  // Cards and diagrams in a document render read-only: a provider with no onReply.
  const readOnly = useMemo(() => ({ messageId: data?.id ?? source.key, roomId: "", authorId: "", threadRootMessageId: null, attachments: [], answered: null }), [data?.id, source.key]);
  return (
    <PaneFrame title={title} actions={openTask}>
      {error && <p role="alert" className="p-4 text-ui">{describeError(error)}</p>}
      {data && <div className="mx-auto max-w-2xl p-4"><h1 className="text-base font-semibold">{title}</h1>
        <CardContext.Provider value={readOnly}><MarkdownView skipHtml components={roomMarkdown}>{data.body ?? ""}</MarkdownView></CardContext.Provider></div>}
    </PaneFrame>
  );
}
```

`PanelProps` is exported from `panelComponents.ts` (check: it is `export interface PanelProps`). If `useApi()` has no `download(room, id)`, use the same function `MessagingWorkspaceContext` hands to `RoomPanel` as `onDownloadAttachment` (grep it) and import from there, adjusting the test mock accordingly.

- [ ] **Step 5: Run tests, typecheck, lint, scanner**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/workspace/panels/__tests__ src/components/workspace/__tests__ && pnpm -F @lightbulb/frontend exec tsc --noEmit -p tsconfig.json && pnpm -F @lightbulb/frontend lint && (cd apps/frontend && node scripts/brand-tokens-scan.mjs)`
Expected: PASS; the existing registry tests still pass with the new kind.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/workspace
git commit -m "pane: an artifact pane opens a room file, a Paperclip artifact or a document in place"
```

---

### Task 4: Chat cards and the Library open the pane; the dialog goes

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomImageAttachments.tsx` (document-like kinds become "open in pane" cards)
- Modify: `apps/frontend/src/components/messaging/RoomDocumentCards.tsx` (open the pane)
- Modify: `apps/frontend/src/components/teams/panels/ArtifactsPanel.tsx` and `ArtifactCards.tsx` (`onOpen` → the pane)
- Delete: `apps/frontend/src/components/teams/panels/ArtifactViewerDialog.tsx`
- Test: `apps/frontend/src/components/messaging/__tests__/RoomImageAttachments.test.tsx` (the download-only table loses xlsx/csv/md/txt; a new case)
- Test: `apps/frontend/src/components/teams/panels/__tests__/ArtifactsPanel.test.tsx` (the viewer expectation becomes an `openPanel` call)

**Interfaces:**
- Consumes: `openPanel` (`workspaceController`), `isPaneViewable` (Task 2), the pane params (Task 3).

- [ ] **Step 1: Update the tests first**

In `RoomImageAttachments.test.tsx`, mock the controller (`vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: vi.fn() }))`), shrink the "explicit opaque download" table to zip, docx and pptx, and add:

```tsx
it.each([
  ["report.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "XLSX"],
  ["data.csv", "text/csv", "CSV"],
  ["page.html", "text/html", "HTML"],
  ["notes.md", "text/markdown", "MD"],
  ["report.pdf", "application/pdf", "PDF"],
] as const)("opens %s in the artifact pane without prefetching it", async (fileName, contentType, label) => {
  const { openPanel } = await import("@/components/workspace/workspaceController");
  const download = vi.fn();
  render(<RoomImageAttachments roomId="room-1" attachments={[{ id: "a1", fileName, contentType, size: 10 }]} download={download} />);
  const card = screen.getByRole("button", { name: new RegExp(`open ${fileName}`, "i") });
  expect(within(card).getByText(label)).toBeInTheDocument();
  expect(download).not.toHaveBeenCalled();
  await userEvent.click(card);
  expect(openPanel).toHaveBeenCalledWith("artifact", { src: "attachment", room: "room-1", id: "a1", name: fileName, type: contentType }, { pushUrl: true });
});
```

Keep the existing PDF "accessible authenticated file card" test only if it still describes behaviour; otherwise replace it with the row above (PDF now opens the pane too).

In `ArtifactsPanel.test.tsx`, replace the assertion that `ArtifactViewerDialog` opens with `expect(openPanel).toHaveBeenCalledWith("artifact", expect.objectContaining({ src: "paperclip" }), { pushUrl: true })` (mock the controller the same way).

- [ ] **Step 2: Run them to see them fail**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/RoomImageAttachments.test.tsx src/components/teams/panels/__tests__/ArtifactsPanel.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Room attachments**

In `RoomImageAttachments.tsx`: keep `DOWNLOAD_ONLY` for `application/zip`, docx and pptx only; add

```tsx
const PANE_LABEL: Partial<Record<MessageAttachmentContentType, string>> = {
  "application/pdf": "PDF",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
  "text/csv": "CSV", "text/html": "HTML", "text/markdown": "MD", "text/plain": "TXT",
};
const opensInPane = (a: MessageAttachment) => isPaneViewable(a.contentType);
```

exclude pane kinds from the prefetch (`missing = attachments.filter(a => !isDownloadOnly(a) && !opensInPane(a) && …)`) and from `viewableAttachments` (the lightbox keeps images and video), and render each pane kind as a card:

```tsx
<button type="button" className="room-link-card room-doc-card" aria-label={`Open ${attachment.fileName}`}
  onClick={() => openPanel("artifact", { src: "attachment", room: roomId, id: attachment.id, name: attachment.fileName, type: attachment.contentType }, { pushUrl: true })}>
  <span className="room-link-card-body">
    <span className="room-doc-card-head"><FileText size={16} aria-hidden="true" /><span className="room-link-card-title">{attachment.fileName}</span></span>
    <span className="room-link-card-meta">{PANE_LABEL[attachment.contentType]} · {formatBytes(attachment.size)}</span>
    <span className="room-doc-card-foot"><span className="room-doc-card-open" aria-hidden="true">Open</span></span>
  </span>
</button>
```

(`formatBytes` is in `components/media/FileViewerContent.tsx`'s module or a shared lib — reuse whichever the file viewer uses; use the spreadsheet icon for xlsx/csv as the chips do today.) Remove the PDF-specific "authenticated file card" branch, which the pane replaces.

- [ ] **Step 4: Document cards and the Library**

`RoomDocumentCards.tsx`: replace the dialog with `openPanel("artifact", { src: "paperclip", kind: "document", issue: issue.id, key: document.key, ident: issue.identifier }, { pushUrl: true })` on click; drop the `ArtifactViewerDialog` import and the `open` state.

`ArtifactsPanel.tsx`: delete the `viewing` state and the dialog render; `onOpen` becomes a function that maps the artifact to pane params by `artifactKind(artifact.id)`: `document` → `{ src: "paperclip", kind: "document", issue: artifact.issue.id, key: documentKeyFromArtifact(artifact) ?? "", ident: artifact.issue.identifier }` (skip when the key is null); `attachment` / `work_product` → `{ src: "paperclip", kind, issue: artifact.issue.id, id: <the attachment id: artifactRawId for an attachment artifact, or the id parsed from downloadPath/openPath via attachmentPathFromHref for a work product>, name: artifact.title, type: artifact.contentType ?? undefined }`. `ArtifactCards.tsx`'s secondary "open in issue" / "download" / "delete" icons stay.

Delete `ArtifactViewerDialog.tsx` and fix every import (`grep -rn ArtifactViewerDialog apps/frontend/src`).

- [ ] **Step 5: Run the tests, the messaging and teams folders, typecheck, lint, scanner**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/RoomImageAttachments.test.tsx src/components/teams/panels/__tests__ && pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__ && pnpm -F @lightbulb/frontend exec tsc --noEmit -p tsconfig.json && pnpm -F @lightbulb/frontend lint && (cd apps/frontend && node scripts/brand-tokens-scan.mjs)`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add -A apps/frontend/src/components/messaging apps/frontend/src/components/teams/panels
git commit -m "chat and Library: files and documents open in the artifact pane; the viewer dialog is gone"
```

---

### Task 5: Mermaid diagrams inline

**Files:**
- Create: `apps/frontend/src/components/messaging/MermaidBlock.tsx`
- Modify: `apps/frontend/src/components/messaging/roomMarkdown.tsx` (the `code` override: a `language-mermaid` branch beside the card one; the `pre` override unwraps it too)
- Test: `apps/frontend/src/components/messaging/__tests__/MermaidBlock.test.tsx`

**Interfaces:**
- Consumes: the `CodeOrCard` / `PreOrCard` overrides from Track A.
- Produces: a ```` ```mermaid ```` fence renders the diagram's SVG inline (in chat and in document bodies); invalid source renders as code with a note.

- [ ] **Step 1: Write the failing test**

```tsx
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown } from "../roomMarkdown";

vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async (_id: string, src: string) => {
      if (src.includes("garbage")) throw new Error("Parse error");
      return { svg: `<svg data-testid="diagram"><text>${src.split("\n")[0]}</text></svg>` };
    }),
  },
}));

it("draws a mermaid fence as an inline diagram", async () => {
  render(<MarkdownView components={roomMarkdown}>{"```mermaid\ngraph TD; A-->B\n```"}</MarkdownView>);
  expect(await screen.findByRole("img", { name: /diagram/i })).toBeInTheDocument();
  expect(document.querySelector("pre")).toBeNull();
});

it("a bad diagram falls back to code", async () => {
  render(<MarkdownView components={roomMarkdown}>{"```mermaid\ngarbage ;;; \n```"}</MarkdownView>);
  expect(await screen.findByText(/couldn't draw this diagram/i)).toBeInTheDocument();
  expect(screen.getByText(/garbage/).closest("pre")).not.toBeNull();
});

it("other fences are untouched", () => {
  render(<MarkdownView components={roomMarkdown}>{"```js\nconsole.log(1)\n```"}</MarkdownView>);
  expect(screen.getByText("console.log(1)").closest("pre")).not.toBeNull();
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/MermaidBlock.test.tsx`
Expected: FAIL (the fence renders as code).

- [ ] **Step 3: The block**

`MermaidBlock.tsx`:

```tsx
"use client";

import { useEffect, useId, useState } from "react";

/** The product's colours for mermaid, read from the CSS variables at render time (mermaid needs real colours; no literals here). */
function themeVariables(): Record<string, string> {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  return {
    primaryColor: v("--plane-raised"), primaryBorderColor: v("--edge"), primaryTextColor: v("--fg"),
    secondaryColor: v("--plane-sidebar"), tertiaryColor: v("--plane-pane"), lineColor: v("--fg-soft"),
    edgeLabelBackground: v("--plane-raised"), fontSize: "13px",
  };
}

let initialised = false;

/** A ```mermaid fence drawn inline. `securityLevel: "strict"` sanitises labels; the SVG is mermaid's own output. */
export function MermaidBlock({ source }: { source: string }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  const [state, setState] = useState<{ svg: string } | { error: string } | null>(null);
  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        if (!initialised) { mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "base", fontFamily: "inherit", themeVariables: themeVariables() }); initialised = true; }
        const { svg } = await mermaid.render(`m${id}`, source);
        if (live) setState({ svg });
      } catch (e) {
        if (live) setState({ error: (e as Error).message });
      }
    })();
    return () => { live = false; };
  }, [id, source]);
  if (!state) return <pre className="rounded-sm p-2 text-caption font-mono" style={{ background: "var(--edge)" }}>{source}</pre>;
  if ("error" in state) return <>
    <p className="text-caption text-muted-foreground">Couldn't draw this diagram.</p>
    <pre className="rounded-sm p-2 text-caption font-mono overflow-x-auto" style={{ background: "var(--edge)" }}>{source}</pre>
  </>;
  // eslint-disable-next-line react/no-danger -- mermaid's sanitised SVG (securityLevel strict), rendered once per source
  return <div role="img" aria-label="Diagram" className="openui-diagram overflow-x-auto my-2" dangerouslySetInnerHTML={{ __html: state.svg }} />;
}
```

If the repo's lint config forbids `dangerouslySetInnerHTML` without the exact disable name used in `LobsterGraphView.tsx`, copy that file's disable comment.

- [ ] **Step 4: Hook it into the overrides**

In `roomMarkdown.tsx`: `const MERMAID_CLASS = "language-mermaid";`, lazy-load `MermaidBlock` the way `OpenUICard` is, and in `CodeOrCard` add — before the default fallthrough and NOT behind the `CardContext` check (a diagram is inert, so a person's mermaid fence may render too) — `if (classes.includes(MERMAID_CLASS)) return <Suspense fallback={<DefaultCode {...props} />}><MermaidBlock source={String(children ?? "").replace(/\n$/, "")} /></Suspense>;`. In `PreOrCard`, unwrap the `<pre>` for `MERMAID_CLASS` as well as the card class.

- [ ] **Step 5: Run the tests, typecheck, lint, scanner**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/MermaidBlock.test.tsx src/components/messaging/__tests__/openuiCard.test.tsx && pnpm -F @lightbulb/frontend exec tsc --noEmit -p tsconfig.json && pnpm -F @lightbulb/frontend lint && (cd apps/frontend && node scripts/brand-tokens-scan.mjs)`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/messaging/MermaidBlock.tsx apps/frontend/src/components/messaging/roomMarkdown.tsx apps/frontend/src/components/messaging/__tests__/MermaidBlock.test.tsx
git commit -m "chat: a mermaid fence draws inline, with the product's colours"
```

---

### Task 6: The workspace file tree shows the same viewers

**Files:**
- Modify: `apps/frontend/src/components/chat/FileContentViewer.tsx` (xlsx / csv / html branches via `FileViewerContent`)
- Test: `apps/frontend/src/components/chat/__tests__/FileContentViewer.types.test.tsx`

**Interfaces:**
- Consumes: `FileViewerContent` with `blob` (Task 2). The tree viewer's `file` object carries `name`, `mime_type`, `size`, `binary`, `content` (base64 for binary, text otherwise).

- [ ] **Step 1: Write the failing test**

Mirror the existing `FileContentViewer` tests' setup (grep the `__tests__` folder for how `file` is supplied) and add: a `text/csv` file with text content renders a table; an xlsx file with base64 content renders a table (build the bytes with SheetJS as in Task 2's test and base64-encode them); a `text/html` file renders a sandboxed frame.

- [ ] **Step 2: Run it to see it fail**, then **Step 3: Implement**

In `FileContentViewer.tsx`, after the PDF branch: build a `Blob` from the file (`file.binary ? Uint8Array.from(atob(file.content), c => c.charCodeAt(0)) : file.content`, with `file.mime_type`) in a `useMemo`, and for `isPaneViewable(file.mime_type) && file.mime_type !== "application/pdf"` render `<FileViewerContent item={{ fileName: file.name, contentType: file.mime_type, size: file.size, url: "", blob }} className="h-full" />` inside the same wrapper the PDF branch uses. Markdown and plain text already render as editable text in this viewer: keep that; only add xlsx, csv and html.

- [ ] **Step 4: Run, typecheck, lint, commit**

```bash
git add apps/frontend/src/components/chat
git commit -m "files pane: spreadsheets and HTML pages preview like chat attachments"
```

---

### Task 7: Dev acceptance and the PR

**Files:** none (receipts on the PR).

- [ ] **Step 1: Open the PR** from the worktree branch with CI green, titled "File artifacts in the pane: spreadsheets, HTML, PDF, diagrams; the Library and documents open in place", body listing the four viewers, the `text/html` guardrails and the deleted dialog.

- [ ] **Step 2: Chrome acceptance on dev** (after the dev deploy of the merge, or on a Vercel preview if the owner prefers), signed in as a dev test account:

1. Ask an agent for "a spreadsheet comparing three email providers" (it attaches an xlsx). The chat shows an XLSX card; Open puts a grid in the pane with the right headers; Download works.
2. Ask for "a one-page landing page as an HTML file". The card opens the page in the pane. Then, in a second request, have it attach this probe page and open it: `<script>document.body.textContent = JSON.stringify({cookie: document.cookie, storage: (()=>{try{localStorage.setItem("x","1");return "allowed"}catch(e){return e.name}})()}); fetch(location.origin + "/api/v1/users/me", {credentials:"include"}).then(r=>document.body.textContent += " api:" + r.status).catch(e=>document.body.textContent += " api:" + e.name)</script>` — expected: empty cookie, `SecurityError`, and `api:TypeError` (no origin, no credentials).
3. Ask for "a PDF summary". Opens inline in the pane.
4. Ask for "a diagram of our signup funnel". Drawn inline in the message. Then paste a broken mermaid fence yourself: code with the note, no crash.
5. Open the Library; click a document, a work product and an attachment: each opens in the pane, with Open task on the document.
6. Copy the pane URL of the spreadsheet, open it in another member's session: it loads; open it signed in as a non-member: "You can't open this file."

Paste the six observations and screenshots on the PR.
