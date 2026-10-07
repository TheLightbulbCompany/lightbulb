import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { SearchResultRow } from "@/components/teams/search/SearchResultRow";
import type {
  CompanySearchIssueSummary,
  CompanySearchResult,
} from "@/components/teams/shared/types";

// The row reads useInlineTeamsNav() but tolerates a null context (falls back to
// its <a href> nav), so no provider wrapper is needed for these render tests.

function issueSummary(overrides: Partial<CompanySearchIssueSummary> = {}): CompanySearchIssueSummary {
  return {
    id: "iss_1",
    identifier: "PAP-80",
    title: "Design the auth flow",
    status: "todo",
    priority: "medium",
    assigneeAgentId: null,
    assigneeUserId: null,
    projectId: null,
    updatedAt: "2026-07-15T00:00:00Z",
    ...overrides,
  };
}

function issueResult(overrides: Partial<CompanySearchResult> = {}): CompanySearchResult {
  const issue = issueSummary();
  return {
    id: issue.id,
    type: "issue",
    score: 1,
    // Upstream builds this as `${identifier} ${title}` — it already prefixes the
    // identifier that the row renders separately.
    title: `${issue.identifier} ${issue.title}`,
    href: "/teams/issues/PAP-80",
    matchedFields: ["title"],
    sourceLabel: "Title",
    snippet: issue.title,
    // The `title`-field highlight ranges are measured against the RAW issue
    // title ("Design the auth flow"): 0..6 covers "Design".
    snippets: [{ field: "title", label: "Title", text: issue.title, highlights: [{ start: 0, end: 6 }] }],
    issue,
    updatedAt: issue.updatedAt,
    previewImageUrl: null,
    ...overrides,
  };
}

describe("SearchResultRow — issue title highlighting", () => {
  it("highlights against the raw issue title, not the identifier-prefixed display string", () => {
    const { container } = render(<SearchResultRow result={issueResult()} />);

    // The highlight range 0..6 must land on "Design" (raw title), proving it is
    // applied to issue.title. If it were applied to result.title
    // ("PAP-80 Design the auth flow"), the same range would highlight "PAP-80".
    const mark = container.querySelector("mark");
    expect(mark).not.toBeNull();
    expect(mark?.textContent).toBe("Design");
  });

  it("does not double-render the identifier", () => {
    const { container } = render(<SearchResultRow result={issueResult()} />);

    // The identifier is rendered once as its own chip. Highlighting result.title
    // (the buggy behavior) would render "PAP-80" a second time inside the title.
    const occurrences = (container.textContent ?? "").split("PAP-80").length - 1;
    expect(occurrences).toBe(1);
  });

  it("renders the raw title text without the identifier prefix", () => {
    const { container } = render(<SearchResultRow result={issueResult()} />);
    // Full display string is never rendered as the title.
    expect(container.textContent).not.toContain("PAP-80 Design the auth flow");
  });
});

describe("SearchResultRow — non-issue rows are unaffected", () => {
  it("a project row still highlights against result.title", () => {
    const projectResult: CompanySearchResult = {
      id: "proj_1",
      type: "project",
      score: 1,
      title: "Website Redesign",
      href: "/teams/projects/proj_1",
      matchedFields: ["title"],
      sourceLabel: "Title",
      snippet: "Website Redesign",
      // 0..7 covers "Website" of result.title (projects carry no identifier prefix).
      snippets: [
        { field: "title", label: "Title", text: "Website Redesign", highlights: [{ start: 0, end: 7 }] },
      ],
      updatedAt: "2026-07-15T00:00:00Z",
      previewImageUrl: null,
    };

    const { container } = render(<SearchResultRow result={projectResult} />);
    const mark = container.querySelector("mark");
    expect(mark?.textContent).toBe("Website");
    expect(container.textContent).toContain("Redesign");
  });
});
