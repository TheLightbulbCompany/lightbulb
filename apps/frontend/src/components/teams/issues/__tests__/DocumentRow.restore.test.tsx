import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// A restore must hit, and then refresh, the routes of the document's OWNER:
// `/issues/{id}` by default, `/cases/{id}` when a case passes `basePath`.
const post = vi.fn();
const mutateByPath = new Map<string, ReturnType<typeof vi.fn>>();
const read = (path: string | null) => {
  if (path === null) return { data: undefined, mutate: vi.fn() };
  if (!mutateByPath.has(path)) mutateByPath.set(path, vi.fn());
  const data = path.endsWith("/revisions")
    ? { revisions: [{ id: "rev-old", key: "plan", revisionNumber: 1, title: null, body: "old", changeSummary: null, createdByAgentId: null, createdByUserId: null, createdAt: "2026-10-01T00:00:00.000Z" }] }
    : { threads: [] };
  return { data, mutate: mutateByPath.get(path)! };
};
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ post, patch: vi.fn(), read }),
}));
// The real menu is a Radix dropdown; the row only needs its "pick a revision".
vi.mock("../DocumentRevisionMenu", () => ({
  DocumentRevisionMenu: ({ onSelectRevision }: { onSelectRevision: (id: string) => void }) => (
    <button type="button" onClick={() => onSelectRevision("rev-old")}>
      pick old revision
    </button>
  ),
}));

import { DocumentRow } from "../DocumentRow";
import type { IssueDocument } from "@/components/teams/shared/types";

const doc = {
  id: "d1",
  key: "plan",
  title: "Plan",
  body: "hello world",
  latestRevisionId: "rev-2",
  latestRevisionNumber: 2,
} as unknown as IssueDocument;

beforeEach(() => {
  post.mockReset().mockResolvedValue({});
  mutateByPath.clear();
});

async function restore() {
  // The key chip sits inside the row's expand button.
  fireEvent.click(screen.getByText("plan"));
  fireEvent.click(screen.getByText("pick old revision"));
  fireEvent.click(screen.getByText("Restore this revision"));
  await waitFor(() => expect(post).toHaveBeenCalled());
}

describe("DocumentRow restore", () => {
  it("restores a CASE document on the case routes and refreshes the case's revision read", async () => {
    const onRestored = vi.fn();
    render(<DocumentRow doc={doc} agents={[]} issueId="c1" basePath="/cases/c1" onRestored={onRestored} />);
    await restore();

    expect(post).toHaveBeenCalledWith("/cases/c1/documents/plan/revisions/rev-old/restore", {});
    await waitFor(() => expect(mutateByPath.get("/cases/c1/documents/plan/revisions")).toHaveBeenCalled());
    expect(onRestored).toHaveBeenCalled();
    expect([...mutateByPath.keys()].some((path) => path.startsWith("/issues/"))).toBe(false);
  });

  it("keeps an issue document on the issue routes", async () => {
    render(<DocumentRow doc={doc} agents={[]} issueId="iss1" />);
    await restore();

    expect(post).toHaveBeenCalledWith("/issues/iss1/documents/plan/revisions/rev-old/restore", {});
    await waitFor(() => expect(mutateByPath.get("/issues/iss1/documents/plan/revisions")).toHaveBeenCalled());
  });
});
