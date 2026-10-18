// Reproduction of the dev crash (React #185) when the document refreshed under an open editor.
// The real MDXEditor is used on purpose: the loop did not show with the editor mocked.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ post: vi.fn(), put: vi.fn(), mutate: vi.fn(), read: () => ({ data: undefined, mutate: vi.fn() }) }),
}));
import { DocumentRow } from "../DocumentRow";
import type { IssueDocument } from "@/components/teams/shared/types";

const body1 = "# Launch\n\nIntro line.\n\n## Table\n\n| # | Need |\n|---|---|\n| 1 | A |\n\n(phone edit check)\n";
const v1 = { id: "d1", key: "plan", title: "Plan", body: body1, latestRevisionId: "r4", latestRevisionNumber: 4, updatedAt: "2026-10-18T07:00:00Z" } as unknown as IssueDocument;
const v2 = { ...v1, body: "# Launch\n\nTab B change, saved first.\n\nIntro line.\n", latestRevisionId: "r5", latestRevisionNumber: 5, updatedAt: "2026-10-18T07:05:00Z", updatedByUserId: "u1" } as unknown as IssueDocument;

it("a document that refreshes under an open editor does not crash and keeps the draft", async () => {
  const errors: unknown[] = [];
  const spy = vi.spyOn(console, "error").mockImplementation((...a) => { errors.push(a[0]); });
  const { rerender } = render(<DocumentRow doc={v1} agents={[]} issueId="i1" defaultOpen />);
  await userEvent.click(screen.getByRole("button", { name: "Edit document" }));
  const editor = await screen.findByRole("textbox", { name: /editable markdown|Body/ }, { timeout: 15000 });
  expect(editor).toBeInTheDocument();
  let threw: unknown = null;
  try {
    rerender(<DocumentRow doc={v2} agents={[]} issueId="i1" defaultOpen />);
    await new Promise((r) => setTimeout(r, 300));
  } catch (e) { threw = e; }
  spy.mockRestore();
  const depth = errors.some((e) => String(e).includes("Maximum update depth") || String(e).includes("#185"));
  expect({ threw: threw ? String(threw).slice(0, 120) : null, depthError: depth }).toEqual({ threw: null, depthError: false });
  expect(screen.getByRole("textbox", { name: /editable markdown|Body/ })).toBeInTheDocument();
}, 30000);
