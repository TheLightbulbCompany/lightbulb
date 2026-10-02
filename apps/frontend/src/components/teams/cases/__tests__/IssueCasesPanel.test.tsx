import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

let issueCases: unknown;

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) => ({
      data: path === "/issues/iss1/cases" ? issueCases : undefined,
      error: null,
      isLoading: false,
      mutate: vi.fn(),
    }),
  }),
}));

import { IssueCasesPanel } from "@/components/teams/cases/IssueCasesPanel";

describe("IssueCasesPanel", () => {
  it("lists the linked cases with their role and status", () => {
    issueCases = {
      cases: [
        {
          id: "link1",
          role: "work",
          createdAt: "2026-10-01T00:00:00.000Z",
          case: { id: "c1", identifier: "LB-C7", title: "Hello world post", caseType: "blog_post", status: "in_review" },
        },
      ],
    };
    render(<IssueCasesPanel issueId="iss1" />);

    expect(screen.getByText("Cases")).toBeInTheDocument();
    expect(screen.getByText("Hello world post").closest("a")).toHaveAttribute(
      "href",
      "/chat?panel=teams-case-detail&detailId=LB-C7",
    );
    expect(screen.getByText("work")).toBeInTheDocument();
    expect(screen.getByText("in review")).toBeInTheDocument();
  });

  it("renders nothing when no cases are linked (or Cases is off)", () => {
    issueCases = undefined;
    const { container } = render(<IssueCasesPanel issueId="iss1" />);
    expect(container).toBeEmptyDOMElement();
  });
});
