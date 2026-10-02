import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

type ReadResult = { data?: unknown; error?: unknown; isLoading: boolean; mutate: () => void };
let casesRead: ReadResult;
/** The unfiltered `/cases?limit=200` read; falls back to `casesRead` when unset. */
let workspaceRead: ReadResult | undefined;
const readPaths: (string | null)[] = [];

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) => {
      readPaths.push(path);
      if (path === "/cases?limit=200" && workspaceRead) return workspaceRead;
      if (path?.startsWith("/cases")) return casesRead;
      return { data: undefined, error: null, isLoading: false, mutate: vi.fn() };
    },
    patch: vi.fn(),
    mutate: vi.fn(),
  }),
}));

import { CasesList } from "@/components/teams/cases/CasesList";

const CASE = {
  id: "11111111-1111-1111-1111-111111111111",
  companyId: "co",
  projectId: null,
  caseNumber: 7,
  identifier: "LB-C7",
  caseType: "blog_post",
  key: "launch/hello",
  title: "Hello world post",
  summary: null,
  status: "in_review",
  fields: {},
  parentCaseId: null,
  createdByAgentId: null,
  createdByUserId: null,
  completedAt: null,
  createdAt: "2026-10-01T00:00:00.000Z",
  updatedAt: "2026-10-01T00:00:00.000Z",
};

beforeEach(() => {
  readPaths.length = 0;
  workspaceRead = undefined;
});

describe("CasesList", () => {
  it("renders cases grouped by type, each row linking to its detail panel", () => {
    casesRead = { data: { cases: [CASE] }, error: null, isLoading: false, mutate: vi.fn() };
    render(<CasesList />);

    // Mobile + desktop markup both carry the title; the row itself is one link.
    const row = screen.getAllByText("Hello world post")[0].closest("a");
    expect(row).toHaveAttribute("href", "/chat?panel=teams-case-detail&detailId=LB-C7");
    expect(screen.getByText("blog_post")).toBeInTheDocument();
    expect(screen.getByText("1 case")).toBeInTheDocument();
    // The default view asks upstream for active cases only.
    expect(readPaths).toContain("/cases?status=active&limit=200");
  });

  it("shows the onboarding hero when the company has no cases", () => {
    casesRead = { data: { cases: [] }, error: null, isLoading: false, mutate: vi.fn() };
    render(<CasesList />);
    expect(screen.getByText("No cases yet")).toBeInTheDocument();
  });

  it("keeps the toolbar when every case is done or cancelled", () => {
    // The default view reads active cases only, so it is empty here; the hero
    // would leave no way to reach the completed ones.
    casesRead = { data: { cases: [] }, error: null, isLoading: false, mutate: vi.fn() };
    workspaceRead = { data: { cases: [{ ...CASE, status: "done" }] }, error: null, isLoading: false, mutate: vi.fn() };
    render(<CasesList />);

    expect(screen.queryByText("No cases yet")).toBeNull();
    expect(screen.getByRole("button", { name: "Filters" })).toBeInTheDocument();
    expect(screen.getByText(/No active cases/)).toBeInTheDocument();
  });

  it("offers every case type in the filter, not only the types in the current result", () => {
    casesRead = { data: { cases: [CASE] }, error: null, isLoading: false, mutate: vi.fn() };
    workspaceRead = {
      data: { cases: [CASE, { ...CASE, id: "22222222-2222-2222-2222-222222222222", caseType: "incident" }] },
      error: null,
      isLoading: false,
      mutate: vi.fn(),
    };
    render(<CasesList />);

    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    expect(screen.getByText("incident")).toBeInTheDocument();
  });

  it("shows the not-enabled state when upstream answers 403", () => {
    casesRead = {
      data: undefined,
      error: Object.assign(new Error("Cases are disabled"), { status: 403 }),
      isLoading: false,
      mutate: vi.fn(),
    };
    render(<CasesList />);
    expect(screen.getByText("Cases are not enabled for this workspace yet")).toBeInTheDocument();
    expect(screen.queryByText("No cases yet")).toBeNull();
  });

  it("keeps any other failure distinct from not-enabled", () => {
    casesRead = {
      data: undefined,
      error: Object.assign(new Error("upstream error"), { status: 502 }),
      isLoading: false,
      mutate: vi.fn(),
    };
    render(<CasesList />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load cases.");
  });
});
