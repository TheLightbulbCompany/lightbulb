import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const CASE_ID = "11111111-1111-1111-1111-111111111111";

const DETAIL = {
  id: CASE_ID,
  companyId: "co",
  projectId: null,
  caseNumber: 7,
  identifier: "LB-C7",
  caseType: "blog_post",
  key: "launch/hello",
  title: "Hello world post",
  summary: null,
  status: "in_review",
  fields: { slug: "hello-world", publish_url: "https://example.com/hello" },
  parentCaseId: null,
  createdByAgentId: null,
  createdByUserId: null,
  completedAt: null,
  createdAt: "2026-10-01T00:00:00.000Z",
  updatedAt: "2026-10-01T00:00:00.000Z",
  parent: null,
  labels: [],
  issueLinks: [
    {
      id: "link1",
      caseId: CASE_ID,
      issueId: "iss1",
      role: "work",
      createdAt: "2026-10-01T00:00:00.000Z",
      issue: { id: "iss1", identifier: "LB-12", title: "Write the post", status: "in_progress" },
    },
  ],
  documents: [],
  attachments: [],
};

let detailRead: { data?: unknown; error?: unknown };

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) => {
      const base = { error: null, isLoading: false, mutate: vi.fn() };
      if (path === "/cases/LB-C7") return { ...base, ...detailRead };
      if (path === "/cases/LB-C7/events?limit=100") {
        return { ...base, data: { events: [{ id: "e1", kind: "created" }] } };
      }
      return { ...base, data: undefined };
    },
    patch: vi.fn(),
    post: vi.fn(),
    mutate: vi.fn(),
    download: vi.fn(),
  }),
}));

import { CaseDetailPage } from "@/components/teams/cases/CaseDetailPage";

describe("CaseDetailPage", () => {
  it("renders the case, its fields and its linked tasks", () => {
    detailRead = { data: DETAIL };
    render(<CaseDetailPage caseId="LB-C7" />);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Hello world post");
    // Identifier + key are click-to-copy tokens.
    expect(screen.getByRole("button", { name: "Copy case ID LB-C7" })).toBeInTheDocument();
    // Fields render generically, by value type: a URL becomes an external link.
    expect(screen.getByText("slug")).toBeInTheDocument();
    expect(screen.getByText("hello-world")).toBeInTheDocument();
    expect(screen.getByTitle("https://example.com/hello")).toHaveAttribute("href", "https://example.com/hello");
    // A linked task opens the task panel.
    expect(screen.getByText("LB-12").closest("a")).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=iss1",
    );
    // The Activity tab carries the event count.
    expect(screen.getByRole("tab", { name: /Activity/ })).toHaveTextContent("1");
  });

  it("shows the case summary on the Overview tab", () => {
    detailRead = { data: { ...DETAIL, summary: "Created to check the Cases panel" } };
    render(<CaseDetailPage caseId="LB-C7" />);
    expect(screen.getByText("Created to check the Cases panel")).toBeInTheDocument();
  });

  it("says so when the case cannot be read", () => {
    detailRead = { data: undefined, error: Object.assign(new Error("Case not found"), { status: 404 }) };
    render(<CaseDetailPage caseId="LB-C7" />);
    expect(screen.getByText("Case not found.")).toBeInTheDocument();
    expect(screen.getByText("← Back to cases")).toHaveAttribute("href", "/chat?panel=teams-cases");
  });
});
