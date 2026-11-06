import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CreationOutput, TaskSummary } from "@lightbulb/shared";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "@/components/tasks/__tests__/fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown, agentNames: new Map(), messaging: { attachmentUrl: vi.fn() } }));
const apps = vi.hoisted(() => ({ apps: [] as unknown[] }));
const ctl = vi.hoisted(() => ({ openPanel: vi.fn(), openChatPanelWithDraft: vi.fn(() => true) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent }));
vi.mock("@/hooks/useApps", () => ({ useApps: () => apps }));
vi.mock("@/components/workspace/workspaceController", () => ctl);
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspaceOptional: () => ({ directoryProps: { rooms: [
    { roomId: "r-walk7", kind: "channel", name: "walk-7", participants: [], isDefault: false },
    { roomId: "r-walk9", kind: "channel", name: "walk-9", participants: [], isDefault: false },
  ] } }),
}));
vi.mock("@/components/home/HomeProjects", () => ({ projectRooms: (rooms: unknown[]) => rooms }));

import { CreationsGrid } from "../CreationsGrid";
import { MadeHere } from "../MadeHere";
import { ProjectFiles } from "../ProjectFiles";
import { TaskOutputs } from "../TaskOutputs";
import { openOutput, outputLink } from "../openOutput";

const out = (over: Partial<CreationOutput>): CreationOutput => ({
  id: "o", kind: "document", title: "Welcome note.md", ref: {}, createdAt: new Date(Date.now() - 3_600_000).toISOString(),
  taskId: "t1", identifier: "LB-1", taskTitle: "Write the welcome", taskStatus: "resolved", specialist: "growth", roomId: "r-walk7", ...over,
});
const APP = out({ id: "app:a1", kind: "app", title: "Walk7 Signup", ref: { appId: "a1" }, taskId: "t-app", specialist: "engineering" });
const DOC = out({ id: "file:f1", kind: "document", title: "Welcome note.md", ref: { roomId: "r-walk7", attachmentId: "att1", fileName: "Welcome note.md", contentType: "text/markdown" } });
const MAIL = out({ id: "effect:m1", kind: "mail", title: "Hello, Priya", taskId: "t-mail", roomId: "r-walk9" });

let client: FakeTasksClient;
beforeEach(() => {
  client = fakeTasksClient([]);
  live.tasks = client;
  apps.apps = [];
  ctl.openPanel.mockReset();
  ctl.openChatPanelWithDraft.mockClear();
  localStorage.clear();
});

describe("openOutput: one opener for every surface", () => {
  it("app → the split view, doc → the file viewer, link → a new tab, mail → its task", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    openOutput(APP);
    openOutput(DOC);
    openOutput(out({ kind: "pr", ref: { url: "https://github.com/acme/web/pull/7" } }));
    const openTask = vi.fn();
    openOutput(MAIL, openTask);
    openOutput(MAIL);
    expect(ctl.openPanel.mock.calls).toEqual([
      ["canvas", { app: "a1" }, { pushUrl: true }],
      ["artifact", { src: "attachment", room: "r-walk7", id: "att1", name: "Welcome note.md", type: "text/markdown" }, { pushUrl: true }],
      ["task-detail", { taskId: "t-mail" }, { pushUrl: true }],
    ]);
    expect(open).toHaveBeenCalledWith("https://github.com/acme/web/pull/7", "_blank", "noopener,noreferrer");
    expect(openTask).toHaveBeenCalledWith("t-mail");
    expect(outputLink(APP)).toBe(`${window.location.origin}/chat?panel=canvas&app=a1`);
    open.mockRestore();
  });

  it("never opens a non-http link", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    openOutput(out({ kind: "link", ref: { url: "javascript:alert(1)" } }));
    expect(open).not.toHaveBeenCalled();
    open.mockRestore();
  });
});

describe("Creations", () => {
  it("lists what tasks made; Mail narrows the read; a project narrows it again", async () => {
    client.outputs.mockResolvedValue({ outputs: [APP, DOC, MAIL], next: null });
    render(<Fresh><CreationsGrid /></Fresh>);
    const grid = await screen.findByRole("list", { name: "Creations" });
    expect(within(grid).getAllByRole("listitem")).toHaveLength(3);
    expect(within(grid).getByText("Doc · walk-7 · 1h")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull(); // no second input
    await userEvent.click(screen.getByRole("button", { name: "Mail" }));
    await waitFor(() => expect(client.outputs).toHaveBeenLastCalledWith(expect.objectContaining({ kind: ["mail"] })));
    await userEvent.click(screen.getByRole("button", { name: "Links" }));
    await waitFor(() => expect(client.outputs).toHaveBeenLastCalledWith(expect.objectContaining({ kind: ["link", "pr"] })));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Project" }), "r-walk9");
    await waitFor(() => expect(client.outputs).toHaveBeenLastCalledWith(expect.objectContaining({ roomId: "r-walk9" })));
  });

  it("a building app says Waiting on its builder in minutes, never Stopped; a live one says nothing", async () => {
    apps.apps = [{ app_id: "a1", name: "walk7-signup", state: "building", stuck: { at: new Date(Date.now() - 5 * 60_000).toISOString(), step: "build" },
      preview_url: null, live_url: null }];
    client.outputs.mockResolvedValue({ outputs: [APP], next: null });
    render(<Fresh><CreationsGrid /></Fresh>);
    expect(await screen.findByText("Waiting on Engineering · 5 min")).toBeInTheDocument();
    expect(screen.queryByText(/Stopped/)).toBeNull();
  });

  it("Make something new › Doc drafts the ask in the Lightbulb chat and sends nothing", async () => {
    render(<Fresh><CreationsGrid /></Fresh>);
    await userEvent.click(screen.getByRole("button", { name: /Make a doc/ }));
    expect(ctl.openChatPanelWithDraft).toHaveBeenCalledWith("Write a doc: …");
    expect(await screen.findByText("Everything Lightbulb and the specialists make lands here.")).toBeInTheDocument();
  });

  it("Load more reads the next page", async () => {
    client.outputs.mockResolvedValueOnce({ outputs: Array.from({ length: 12 }, (_, i) => out({ id: `o${i}`, taskId: `t${i}` })), next: "c2" })
      .mockResolvedValueOnce({ outputs: [MAIL], next: null });
    render(<Fresh><CreationsGrid /></Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await waitFor(() => expect(within(screen.getByRole("list", { name: "Creations" })).getAllByRole("listitem")).toHaveLength(13));
    expect(client.outputs).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: "c2" }));
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("an error offers Retry", async () => {
    client.outputs.mockRejectedValueOnce(new Error("down"));
    render(<Fresh><CreationsGrid /></Fresh>);
    expect(await screen.findByRole("alert")).toHaveTextContent("Creations couldn't load.");
  });
});

describe("Made here", () => {
  it("is hidden while the project has made nothing", async () => {
    const { container } = render(<Fresh><MadeHere roomId="r-walk7" onOpenTask={vi.fn()} onAllFiles={vi.fn()} /></Fresh>);
    await waitFor(() => expect(client.outputs).toHaveBeenCalledWith(expect.objectContaining({ roomId: "r-walk7", limit: 4 })));
    expect(container).toBeEmptyDOMElement();
  });

  it("shows up to four tiles named by title; a mail tile opens its task here; All files switches the view", async () => {
    client.outputs.mockResolvedValue({ outputs: [APP, DOC, MAIL, out({ id: "x" }), out({ id: "y" })], next: null });
    const onOpenTask = vi.fn();
    const onAllFiles = vi.fn();
    render(<Fresh><MadeHere roomId="r-walk7" onOpenTask={onOpenTask} onAllFiles={onAllFiles} /></Fresh>);
    const strip = await screen.findByRole("region", { name: "Made here" });
    expect(within(strip).getAllByRole("listitem")).toHaveLength(4);
    await userEvent.click(within(strip).getByRole("button", { name: "Open Hello, Priya" }));
    expect(onOpenTask).toHaveBeenCalledWith("t-mail");
    await userEvent.click(within(strip).getByRole("button", { name: /All files/ }));
    expect(onAllFiles).toHaveBeenCalled();
  });
});

describe("Files", () => {
  it("an empty project shows the tip at the foot; dismissed, it stays dismissed", async () => {
    const { unmount } = render(<Fresh><ProjectFiles roomId="r-walk7" viewerId="u1" messages={[]} onOpenTask={vi.fn()} /></Fresh>);
    const tip = await screen.findByRole("complementary", { name: "Tip" });
    expect(tip).toHaveTextContent("Ask for a doc, an app or a file in any task. It lands here.");
    await userEvent.click(within(tip).getByRole("button", { name: "Dismiss tip" }));
    unmount();
    render(<Fresh><ProjectFiles roomId="r-walk7" viewerId="u1" messages={[]} onOpenTask={vi.fn()} /></Fresh>);
    await waitFor(() => expect(client.outputs).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("complementary", { name: "Tip" })).toBeNull();
  });

  it("lists outputs and shared files with Made by; the type select narrows the read", async () => {
    client.outputs.mockResolvedValue({ outputs: [DOC], next: null });
    const msg = { messageId: "m1", createdAt: new Date().toISOString(), author: { kind: "human", id: "p", displayName: "Priya" },
      attachments: [{ id: "att9", fileName: "logo.png", contentType: "image/png", size: 1 }] };
    render(<Fresh><ProjectFiles roomId="r-walk7" viewerId="u1" messages={[msg] as never} onOpenTask={vi.fn()} /></Fresh>);
    const table = await screen.findByRole("table");
    expect(within(table).getByRole("row", { name: /Welcome note/ })).toHaveTextContent("Growth");
    expect(within(table).getByRole("row", { name: /logo\.png/ })).toHaveTextContent("Priya (shared)");
    expect(screen.queryByRole("complementary", { name: "Tip" })).toBeNull();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Type" }), "apps");
    await waitFor(() => expect(client.outputs).toHaveBeenLastCalledWith(expect.objectContaining({ kind: ["app"] })));
  });
});

describe("TaskOutputs", () => {
  const task = { taskId: "t1", outputs: [APP, DOC, MAIL] } as Pick<TaskSummary, "taskId" | "outputs">;

  it("shows two chips then +N more, which opens the task", async () => {
    const onOpenTask = vi.fn();
    render(<TaskOutputs task={task} max={2} onOpenTask={onOpenTask} />);
    expect(screen.getAllByRole("button", { name: /^Open / })).toHaveLength(2);
    await userEvent.click(screen.getByRole("button", { name: "+1 more" }));
    expect(onOpenTask).toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Open Walk7 Signup" }));
    expect(ctl.openPanel).toHaveBeenCalledWith("canvas", { app: "a1" }, { pushUrl: true });
  });

  it("in the open task, a mail chip is a label (its record is this task)", () => {
    render(<TaskOutputs task={task} />);
    expect(screen.getByText("Hello, Priya")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open Hello, Priya" })).toBeNull();
    expect(screen.getAllByRole("button")).toHaveLength(2);
  });
});
