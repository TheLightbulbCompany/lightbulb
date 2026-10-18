import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ post: vi.fn(), patch: vi.fn(), read: () => ({ data: undefined, mutate: vi.fn() }) }),
}));

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
});
