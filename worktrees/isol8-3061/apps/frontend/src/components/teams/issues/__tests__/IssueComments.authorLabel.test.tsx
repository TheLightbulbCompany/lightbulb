import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { IssueComments } from "@/components/teams/issues/IssueComments";
import type {
  CompanyAgent,
  IssueComment,
  RequestConfirmationInteraction,
} from "@/components/teams/shared/types";

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

describe("interaction card actor labels", () => {
  function interaction(
    overrides: Partial<RequestConfirmationInteraction>,
  ): RequestConfirmationInteraction {
    return {
      id: "int_1",
      companyId: "co_1",
      issueId: "iss_1",
      kind: "request_confirmation",
      status: "accepted",
      continuationPolicy: "none",
      createdAt: "2026-07-15T00:00:00Z",
      updatedAt: "2026-07-15T00:00:00Z",
      createdByAgentId: "agt_friday",
      payload: { version: 1, prompt: "Ship it?" },
      ...overrides,
    };
  }

  it("shows the enriched email for a human resolver", () => {
    render(
      <IssueComments
        comments={[]}
        agents={AGENTS}
        onSubmit={async () => {}}
        interactions={[
          interaction({
            resolvedByUserId: "pcu_1",
            resolvedByEmail: "ada@example.com",
            resolvedByClerkUserId: "u_ada",
          }),
        ]}
      />,
    );
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
  });

  it('shows "You" when the viewer resolved it', () => {
    render(
      <IssueComments
        comments={[]}
        agents={AGENTS}
        onSubmit={async () => {}}
        currentUserId="u_ada"
        interactions={[
          interaction({
            resolvedByUserId: "pcu_1",
            resolvedByEmail: "ada@example.com",
            resolvedByClerkUserId: "u_ada",
          }),
        ]}
      />,
    );
    expect(screen.getByText("You")).toBeInTheDocument();
  });

  it('falls back to "User" (never an id prefix) for an unenriched resolver', () => {
    render(
      <IssueComments
        comments={[]}
        agents={AGENTS}
        onSubmit={async () => {}}
        interactions={[interaction({ resolvedByUserId: "user_abcdef123" })]}
      />,
    );
    expect(screen.getByText("User")).toBeInTheDocument();
    expect(screen.queryByText("user_")).not.toBeInTheDocument();
  });
});
