import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, expect, it, vi } from "vitest";

const downloadBlob = vi.fn();
const teamsRead = vi.fn();
const teamsDownload = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn(), downloadBlob }) }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: teamsRead, download: teamsDownload }) }));
vi.mock("@/components/workspace/workspaceController", () => ({ closePane: vi.fn(), openPanel: vi.fn(), updatePanelParams: vi.fn() }));
const superAgent = { on: false };
vi.mock("@/hooks/useLiveEvents", () => ({ useSuperAgent: () => superAgent.on }));
import { ArtifactDockPanel } from "../ArtifactDockPanel";
import { PaneActionsHost } from "../../PaneHeader";

beforeAll(() => {
  URL.createObjectURL = vi.fn(() => "blob:x");
  URL.revokeObjectURL = vi.fn();
});

it("opens a room CSV attachment in the grid", async () => {
  downloadBlob.mockResolvedValue(new Blob(["a,b\n1,2\n"], { type: "text/csv" }));
  render(<ArtifactDockPanel id="artifact" params={{ src: "attachment", room: "r1", id: "att1", name: "out.csv", type: "text/csv" }} />);
  expect(await screen.findByRole("table")).toBeInTheDocument();
  // The title lives in RightPane's bar (describePanel); the pane draws no second header.
  expect(screen.queryByRole("heading", { name: "out.csv" })).toBeNull();
  expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute("download", "out.csv");
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
  expect(await screen.findByRole("heading", { name: "Launch" })).toBeInTheDocument();
  expect(screen.getByText("Step one.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /open task/i })).toBeInTheDocument();
});

it("refetches only when the source really changes", async () => {
  downloadBlob.mockReset();
  downloadBlob.mockResolvedValue(new Blob(["a,b\n1,2\n"], { type: "text/csv" }));
  const mk = (id: string) => ({ src: "attachment", room: "r1", id, name: "out.csv", type: "text/csv" });
  const { rerender } = render(<ArtifactDockPanel id="artifact" params={mk("att1")} />);
  await screen.findByRole("table");
  rerender(<ArtifactDockPanel id="artifact" params={mk("att1")} />);
  expect(downloadBlob).toHaveBeenCalledTimes(1);
  expect(URL.revokeObjectURL).not.toHaveBeenCalled();
  rerender(<ArtifactDockPanel id="artifact" params={mk("att2")} />);
  await screen.findByRole("table");
  expect(downloadBlob).toHaveBeenCalledTimes(2);
});

it("the viewer type is the served type, never the URL's: html bytes behind type=pdf are not framed by src", async () => {
  vi.mocked(URL.createObjectURL).mockClear();
  downloadBlob.mockResolvedValue(new Blob(["<script>parent.x=1</script>"], { type: "text/html; charset=utf-8" }));
  const { container } = render(<ArtifactDockPanel id="artifact" params={{ src: "attachment", room: "r1", id: "evil", name: "report.pdf", type: "application/pdf" }} />);
  await screen.findByTitle("report.pdf");
  const made = vi.mocked(URL.createObjectURL).mock.calls.map(([b]) => (b as Blob).type);
  expect(made).toEqual(["text/html"]);
  const frames = [...container.querySelectorAll("iframe")];
  expect(frames).toHaveLength(1);
  expect(frames[0]).not.toHaveAttribute("src");
  expect(frames[0]).toHaveAttribute("sandbox", "allow-scripts");
});

it("an untyped response previews nothing and offers Download", async () => {
  downloadBlob.mockResolvedValue(new Blob(["%PDF-1.4"]));
  const { container } = render(<ArtifactDockPanel id="artifact" params={{ src: "attachment", room: "r1", id: "u1", name: "x.pdf", type: "application/pdf" }} />);
  expect(await screen.findByText(/no preview/i)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Download" })).toBeInTheDocument();
  expect(container.querySelector("iframe")).toBeNull();
});

it("encodes every path segment taken from the URL", async () => {
  downloadBlob.mockResolvedValue(new Blob(["a"], { type: "text/plain" }));
  render(<ArtifactDockPanel id="artifact" params={{ src: "attachment", room: "../../x?", id: "a/b", name: "n.txt" }} />);
  await screen.findByRole("link", { name: "Download" });
  expect(downloadBlob).toHaveBeenLastCalledWith("/messaging/rooms/..%2F..%2Fx%3F/attachments/a%2Fb");
  teamsDownload.mockResolvedValue(new Blob(["a"], { type: "text/plain" }));
  render(<ArtifactDockPanel id="artifact" params={{ src: "paperclip", kind: "attachment", issue: "../i", id: "a?b" }} />);
  await vi.waitFor(() => expect(teamsDownload).toHaveBeenCalledWith("/issues/..%2Fi/attachments/a%3Fb/content"));
  teamsRead.mockReturnValue({ data: undefined });
  render(<ArtifactDockPanel id="artifact" params={{ src: "paperclip", kind: "document", issue: "../i", key: "k/1" }} />);
  expect(teamsRead).toHaveBeenCalledWith("/issues/..%2Fi/documents/k%2F1");
});

it("super-agent: a text doc's Copy and Download sit in the pane header, not on a row of their own (W14)", async () => {
  superAgent.on = true;
  try {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    downloadBlob.mockResolvedValue(new Blob(["# Menu\n\nRye on Thursday."], { type: "text/markdown" }));
    const host = document.createElement("span");
    document.body.appendChild(host);
    render(<PaneActionsHost.Provider value={host}>
      <ArtifactDockPanel id="artifact" params={{ src: "attachment", room: "r1", id: "att9", name: "menu.md", type: "text/markdown" }} />
    </PaneActionsHost.Provider>);
    const header = within(host);
    expect(await header.findByRole("link", { name: /^Download menu\.md/ })).toHaveAttribute("download", "menu.md");
    await userEvent.click(header.getByRole("button", { name: "Copy text" }));
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith("# Menu\n\nRye on Thursday."));
    expect(await header.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    host.remove();
  } finally { superAgent.on = false; }
});
