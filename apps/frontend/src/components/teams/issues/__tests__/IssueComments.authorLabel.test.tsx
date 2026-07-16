import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { IssueComments } from "@/components/teams/issues/IssueComments";
import type { CompanyAgent, IssueComment } from "@/components/teams/shared/types";

// MarkdownView pulls in the full markdown pipeline; the author-label rows
// under test only need the body rendered as text.
vi.mock("@/components/teams/shared/components/MarkdownView", () => ({
  MarkdownView: ({ children }: { children: string }) => <div>{children}</div>,
}));

const AGENTS: CompanyAgent[] = [{ id: "agt_friday", name: "Friday" }];

function comment(overrides: Partial<IssueComment>): IssueComment {
  return {
    id: "cmt_1",
    body: "hello",
    createdAt: "2026-07-15T00:00:00Z",
    ...overrides,
  };
}

function renderComments(comments: IssueComment[], currentUserId?: string | null) {
  return render(
    <IssueComments
      comments={comments}
      agents={AGENTS}
      onSubmit={async () => {}}
      currentUserId={currentUserId}
    />,
  );
}

describe("IssueComments author labels", () => {
  it("resolves agent authors to the agent name", () => {
    renderComments([comment({ authorAgentId: "agt_friday" })]);
    expect(screen.getByText("Friday")).toBeInTheDocument();
  });

  it("renders the BFF-enriched name for a human author", () => {
    renderComments([
      comment({ authorUserId: "pcu_1", authorName: "Ada Lovelace", authorClerkUserId: "u_ada" }),
    ]);
    expect(screen.getByText("Ada Lovelace")).toBeInTheDocument();
  });

  it('renders "You" when the author is the viewer', () => {
    renderComments(
      [comment({ authorUserId: "pcu_1", authorName: "Ada Lovelace", authorClerkUserId: "u_ada" })],
      "u_ada",
    );
    expect(screen.getByText("You")).toBeInTheDocument();
    expect(screen.queryByText("Ada Lovelace")).not.toBeInTheDocument();
  });

  it('falls back to "User" for a human author with no enrichment', () => {
    renderComments([comment({ authorUserId: "pcu_unknown" })]);
    expect(screen.getByText("User")).toBeInTheDocument();
  });
});
