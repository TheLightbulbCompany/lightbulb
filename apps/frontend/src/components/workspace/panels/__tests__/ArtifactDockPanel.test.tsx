import { render, screen } from "@testing-library/react";
import { beforeAll, expect, it, vi } from "vitest";

const downloadBlob = vi.fn();
const teamsRead = vi.fn();
const teamsDownload = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn(), downloadBlob }) }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: teamsRead, download: teamsDownload }) }));
vi.mock("@/components/workspace/workspaceController", () => ({ closePane: vi.fn(), openPanel: vi.fn() }));
import { ArtifactDockPanel } from "../ArtifactDockPanel";

beforeAll(() => {
  URL.createObjectURL = vi.fn(() => "blob:x");
  URL.revokeObjectURL = vi.fn();
});

it("opens a room CSV attachment in the grid", async () => {
  downloadBlob.mockResolvedValue(new Blob(["a,b\n1,2\n"], { type: "text/csv" }));
  render(<ArtifactDockPanel id="artifact" params={{ src: "attachment", room: "r1", id: "att1", name: "out.csv", type: "text/csv" }} />);
  expect(await screen.findByRole("table")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "out.csv" })).toBeInTheDocument();
  expect(downloadBlob).toHaveBeenCalledWith("/messaging/rooms/r1/attachments/att1");
});

it("a refused fetch shows the error card", async () => {
  downloadBlob.mockRejectedValue(Object.assign(new Error("forbidden"), { status: 403 }));
  render(<ArtifactDockPanel id="artifact" params={{ src: "attachment", room: "r1", id: "att1", name: "out.csv", type: "text/csv" }} />);
  expect(await screen.findByRole("alert")).toHaveTextContent(/can't open this file/i);
});

it("renders a Paperclip document body as markdown with an Open task action", async () => {
  teamsRead.mockReturnValue({ data: { id: "d", key: "plan", title: "Launch plan", body: "# Launch\n\nStep one.", format: "markdown" } });
  render(<ArtifactDockPanel id="artifact" params={{ src: "paperclip", kind: "document", issue: "i1", key: "plan", ident: "ISOA-9" }} />);
  expect(await screen.findByRole("heading", { name: "Launch plan" })).toBeInTheDocument();
  expect(screen.getByText("Step one.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /open task/i })).toBeInTheDocument();
});
