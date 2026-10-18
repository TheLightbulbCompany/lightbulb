import { render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const reads: Record<string, unknown> = {};
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: (path: string | null) => ({ data: path ? reads[path] : undefined, mutate: vi.fn() }), post: vi.fn(), download: vi.fn() }) }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ download: vi.fn() }) }));
vi.mock("@/components/workspace/workspaceController", () => ({ closePane: vi.fn(), openPanel: vi.fn() }));
import { ArtifactDockPanel } from "../ArtifactDockPanel";

const body = ["# Launch plan", "", "Open with the answer.", "", "## Goal", "", "x", "", "## Who", "", "y", "", "## When", "", "z", "", "## Risks", "", "w"].join("\n");

it("shows author, date and version, and a contents list from headings", async () => {
  reads["/issues/i1/documents/plan"] = { id: "d1", key: "plan", title: "Launch plan", body, updatedByAgentId: "a1", updatedAt: "2026-10-16T10:00:00Z", latestRevisionId: "r3", latestRevisionNumber: 3 };
  reads["/agents"] = { agents: [{ id: "a1", name: "Growth" }] };
  reads["/members"] = { members: [] };
  render(<ArtifactDockPanel id="artifact" params={{ src: "paperclip", kind: "document", issue: "i1", key: "plan", ident: "ISOA-850" }} />);
  expect(await screen.findByRole("heading", { name: "Launch plan", level: 2 })).toBeInTheDocument();
  expect(screen.getByText(/Growth · updated .* · rev 3/)).toBeInTheDocument();
  const contents = screen.getByRole("navigation", { name: "Contents" });
  expect(within(contents).getAllByRole("button").map((b) => b.textContent)).toEqual(["Launch plan", "Goal", "Who", "When", "Risks"]);
  expect(screen.getByRole("button", { name: /open task/i })).toBeInTheDocument();
});

it("hides the contents list under four headings", async () => {
  reads["/issues/i1/documents/short"] = { id: "d2", key: "short", title: "Short", body: "# A\n\n## B\n\ntext", updatedAt: "2026-10-16T10:00:00Z", latestRevisionNumber: 1 };
  render(<ArtifactDockPanel id="artifact" params={{ src: "paperclip", kind: "document", issue: "i1", key: "short" }} />);
  await screen.findByRole("heading", { name: "Short", level: 2 });
  expect(screen.queryByRole("navigation", { name: "Contents" })).toBeNull();
});
