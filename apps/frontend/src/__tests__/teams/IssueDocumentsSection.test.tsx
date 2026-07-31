import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { IssueDocumentsSection } from "@/components/teams/issues/IssueDocumentsSection";

// Each document row carries more than one button since the plan-doc review
// affordances landed (expand toggle + comments toggle + revision menu), so
// scope every button query to its own <li> row instead of the whole section.
function expandToggles(): HTMLElement[] {
  return screen
    .getAllByRole("listitem")
    .map((row) => within(row).getAllByRole("button")[0]);
}

function doc(id: string, key: string, body: string, updatedAt: string, title: string | null = null) {
  return { id, key, body, title, updatedAt, createdAt: updatedAt };
}

beforeEach(() => read.mockReset());

describe("IssueDocumentsSection", () => {
  it("renders nothing when there are no documents", () => {
    read.mockReturnValue({ data: { documents: [] }, isLoading: false, error: null, mutate: vi.fn() });
    const { container } = render(<IssueDocumentsSection issueId="iss1" agents={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders documents newest-first with key badge + title + updated time, collapsed by default", () => {
    read.mockReturnValue({
      data: { documents: [
        doc("d-old", "report", "# Old body heading", "2026-05-01T00:00:00Z", "Q2 Earnings"),
        doc("d-new", "plan", "## New body heading", "2026-05-03T00:00:00Z"),
      ] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueDocumentsSection issueId="iss1" agents={[]} />);
    expect(screen.getByText(/Documents/)).toBeInTheDocument();
    const toggles = expandToggles();
    expect(toggles[0]).toHaveTextContent(/plan/i);   // newest first
    expect(toggles[1]).toHaveTextContent(/report/i);
    expect(screen.getByText("Q2 Earnings")).toBeInTheDocument();   // title shown when present & != key
    expect(screen.queryByText("New body heading")).toBeNull(); // collapsed → body not in DOM
    expect(screen.queryByText("Old body heading")).toBeNull();
  });

  it("hides the title when it case-insensitively matches the key", () => {
    read.mockReturnValue({
      data: { documents: [doc("d1", "report", "body", "2026-05-01T00:00:00Z", "Report")] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueDocumentsSection issueId="iss1" agents={[]} />);
    // the redundant title "Report" (≈ key "report") is hidden…
    expect(screen.queryByText("Report")).toBeNull();
    // …but the key badge ("report") still renders
    expect(expandToggles()[0]).toHaveTextContent(/report/i);
  });

  it("expands a document to render its markdown body", () => {
    read.mockReturnValue({
      data: { documents: [doc("d1", "plan", "## Hello section", "2026-05-01T00:00:00Z")] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueDocumentsSection issueId="iss1" agents={[]} />);
    expect(screen.queryByText("Hello section")).toBeNull();
    fireEvent.click(expandToggles()[0]);
    expect(screen.getByText("Hello section")).toBeInTheDocument();
  });
});
