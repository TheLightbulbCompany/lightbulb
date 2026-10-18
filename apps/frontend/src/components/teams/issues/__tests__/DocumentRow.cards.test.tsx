import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ download: () => Promise.resolve(new Blob(['x'])), post: vi.fn(), patch: vi.fn(), read: () => ({ data: undefined, mutate: vi.fn() }) }),
}));

import { AttachmentIssueContext } from "@/components/teams/shared/lib/attachments";
import { DocumentRow } from "../DocumentRow";
import type { IssueDocument } from "@/components/teams/shared/types";

const doc = {
  id: "d1",
  key: "plan",
  title: "Launch plan",
  body: "Pick one:\n\n```openui-lang\nroot = Stack([q])\nq = Choice(\"Which first?\", [\"Starter\", \"Studio\"])\n```\n",
} as unknown as IssueDocument;

describe("DocumentRow cards", () => {
  it("renders a card in a document read-only", async () => {
    render(<DocumentRow doc={doc} agents={[]} issueId="i1" defaultOpen />);
    const button = await screen.findByRole("button", { name: "Studio" });
    expect(button).toBeDisabled();
    expect(screen.queryByText(/root = Stack/)).toBeNull();
  });

  it("keeps attachment links and images working next to a card", async () => {
    const withFiles = { ...doc, body: "[spec.pdf](/api/attachments/att_1/content)\n\n![chart](/api/attachments/att_2/content)\n\n![logo](https://example.com/logo.png)" } as unknown as IssueDocument;
    render(
      <AttachmentIssueContext.Provider value="i1">
        <DocumentRow doc={withFiles} agents={[]} issueId="i1" defaultOpen />
      </AttachmentIssueContext.Provider>,
    );
    expect(await screen.findByRole("button", { name: "spec.pdf" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "logo" })).toHaveAttribute("src", "https://example.com/logo.png");
    expect(screen.queryByText("Image omitted")).toBeNull();
  });
});
