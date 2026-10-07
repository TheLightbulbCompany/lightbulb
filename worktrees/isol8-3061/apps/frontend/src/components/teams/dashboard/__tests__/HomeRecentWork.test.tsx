import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));
vi.mock("@/lib/shellNav", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/shellNav")>();
  return { ...mod, handleShellLinkClick: vi.fn() };
});

import { HomeRecentWork, RECENT_WORK_PATH } from "../HomeRecentWork";
import { useTeamsApi } from "@/hooks/useTeamsApi";

const artifact = (n: number) => ({
  id: `a${n}`, source: "document", mediaKind: "document", title: `Weekly digest ${n}`,
  issue: { id: `iss${n}`, identifier: `LUL-${n}`, title: "Nightly CEO progress" },
  createdByAgent: { id: "ag1", name: "Friday" }, updatedAt: "2026-09-05T08:10:00Z", href: `/x/${n}`,
});

function mockRead(result: { data?: unknown; error?: unknown; isLoading?: boolean }) {
  const read = vi.fn(() => ({ data: result.data, error: result.error ?? null, isLoading: result.isLoading ?? false, mutate: vi.fn(), isValidating: false }));
  vi.mocked(useTeamsApi).mockReturnValue({ read } as unknown as ReturnType<typeof useTeamsApi>);
  return read;
}

describe("HomeRecentWork", () => {
  beforeEach(() => vi.clearAllMocks());

  it("reads the six newest deliverables and links each to its task", () => {
    const read = mockRead({ data: { artifacts: [artifact(1), artifact(2)], nextCursor: null } });
    render(<HomeRecentWork />);
    expect(read).toHaveBeenCalledWith(RECENT_WORK_PATH);
    const section = screen.getByRole("region", { name: "Recent work" });
    const row = within(section).getByRole("link", { name: /Weekly digest 1/ });
    expect(row).toHaveAttribute("href", expect.stringContaining("iss1"));
    expect(within(row).getByText(/Friday · Nightly CEO progress/)).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Open Library" })).toHaveAttribute("href", expect.stringContaining("teams-artifacts"));
  });

  it("is calm when nothing has been made yet", () => {
    mockRead({ data: { artifacts: [], nextCursor: null } });
    render(<HomeRecentWork />);
    expect(screen.getByRole("status")).toHaveTextContent("Nothing made yet. The first deliverable your agents finish lands here.");
  });

  it("never shows stale rows over a failed read", () => {
    mockRead({ data: { artifacts: [artifact(1)], nextCursor: null }, error: new Error("boom") });
    render(<HomeRecentWork />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't read the Library just now.");
    expect(screen.queryByText(/Weekly digest/)).not.toBeInTheDocument();
  });

  it("shows a loading line, not an empty verdict, while in flight", () => {
    mockRead({ isLoading: true });
    render(<HomeRecentWork />);
    expect(screen.getByRole("status")).toHaveTextContent("Gathering recent work…");
  });
});
