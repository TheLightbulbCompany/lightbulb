import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

vi.mock("@/components/teams/shared/components/MarkdownEditor", () => ({
  MarkdownEditor: ({ value }: { value: string }) => <textarea aria-label="Body" defaultValue={value} />,
}));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ post: vi.fn(), put: vi.fn(), mutate: vi.fn(), read: () => ({ data: undefined, mutate: vi.fn() }) }),
}));
import { DocumentRow } from "../DocumentRow";
import type { IssueDocument } from "@/components/teams/shared/types";

const doc = { id: "d1", key: "plan", title: "Plan", body: "Hello there", latestRevisionId: "r1", latestRevisionNumber: 1 } as unknown as IssueDocument;

it("the pencil opens the editor and Cancel restores the body", async () => {
  render(<DocumentRow doc={doc} agents={[]} issueId="i1" defaultOpen />);
  await userEvent.click(screen.getByRole("button", { name: "Edit document" }));
  expect(await screen.findByRole("textbox", { name: "Body" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByText("Hello there")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "Body" })).toBeNull();
});

it("a case document has no pencil", () => {
  render(<DocumentRow doc={doc} agents={[]} issueId="c1" basePath="/cases/c1" defaultOpen />);
  expect(screen.queryByRole("button", { name: "Edit document" })).toBeNull();
});

it("a locked document has no pencil", () => {
  render(<DocumentRow doc={{ ...doc, lockedAt: "2026-10-16T10:00:00Z" }} agents={[]} issueId="i1" defaultOpen />);
  expect(screen.queryByRole("button", { name: "Edit document" })).toBeNull();
});
