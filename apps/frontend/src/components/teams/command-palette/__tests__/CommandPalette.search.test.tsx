// W12-3: ⌘K finds tasks and what they made (server-filtered by `q`), opens each in its place, and names what it
// searches. Paperclip's Issues group stays out of super-agent workspaces.

import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const searchTasks = vi.fn();
const outputs = vi.fn();
const openTaskPane = vi.fn();
const openOutput = vi.fn();
let superAgent = true;

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/hooks/useLiveEvents", () => ({
  useSuperAgent: () => superAgent,
  useLiveEvents: () => ({ tasks: tasksClient, messaging: messagingClient }),
}));
const tasksClient = { searchTasks, outputs };
const downloadAttachment = vi.fn();
let messagingClient: { downloadAttachment: typeof downloadAttachment } | undefined;
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => false }));
vi.mock("../useFilteredCommandResults", () => ({
  useFilteredCommandResults: () => ({ agents: [], issues: [{ id: "i1", title: "bakery issue" }], projects: [] }),
}));
vi.mock("@/components/messaging/roomCommands", () => ({ useRoomCommandResults: () => [] }));
vi.mock("@/components/messaging/useMessageSearch", () => ({ useMessageSearch: () => ({ hits: [], state: "idle" }) }));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({ useMessagingWorkspaceOptional: () => null }));
vi.mock("@/components/workspace/workspaceController", () => ({ openTaskPane: (id: string) => openTaskPane(id) }));
vi.mock("@/components/creations/openOutput", () => ({
  KIND_WORD: { document: "Doc" },
  openOutput: (o: unknown) => openOutput(o),
}));

import { CommandPalette, wholeWordFirst } from "../CommandPalette";

const doc = { id: "file:f1", kind: "document", title: "bakery.md", ref: {}, createdAt: "1", taskId: "t1" };

beforeEach(() => {
  superAgent = true;
  messagingClient = undefined;
  vi.clearAllMocks();
  searchTasks.mockResolvedValue({ tasks: [{ taskId: "t1", title: "Bakery taglines" }], next: null });
  outputs.mockResolvedValue({ outputs: [doc], next: null });
});

describe("command palette search (super-agent)", () => {
  test("finds tasks and files by name and opens each where it lives", async () => {
    render(<CommandPalette open onOpenChange={() => {}} />);
    const input = screen.getByPlaceholderText("Search projects, tasks, files and messages");
    fireEvent.change(input, { target: { value: "bakery" } });

    fireEvent.click(await screen.findByText("Bakery taglines"));
    expect(searchTasks).toHaveBeenCalledWith("bakery", 6, expect.any(AbortSignal));
    expect(outputs).toHaveBeenCalledWith({ q: "bakery", limit: 6 }, expect.any(AbortSignal));
    expect(openTaskPane).toHaveBeenCalledWith("t1");
    expect(screen.queryByText("bakery issue")).not.toBeInTheDocument();
  });

  test("a file opens in its viewer", async () => {
    render(<CommandPalette open onOpenChange={() => {}} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "bakery" } });
    fireEvent.click(await screen.findByText("bakery.md"));
    expect(openOutput).toHaveBeenCalledWith(doc);
  });

  test("typing on aborts the request for the older query", async () => {
    searchTasks.mockImplementation(() => new Promise(() => {}));
    render(<CommandPalette open onOpenChange={() => {}} />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "bak" } });
    await waitFor(() => expect(searchTasks).toHaveBeenCalledTimes(1));
    const first = searchTasks.mock.calls[0][2] as AbortSignal;
    fireEvent.change(input, { target: { value: "bakery" } });
    expect(first.aborted).toBe(true);
    await waitFor(() => expect(searchTasks).toHaveBeenLastCalledWith("bakery", 6, expect.any(AbortSignal)));
  });

  test("without the super-agent nothing asks the task routes", async () => {
    superAgent = false;
    render(<CommandPalette open onOpenChange={() => {}} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "bakery" } });
    expect(await screen.findByText("bakery issue")).toBeInTheDocument();
    await waitFor(() => expect(searchTasks).not.toHaveBeenCalled());
  });

  test("a doc is found by a word in its text, read once through the ranged attachment read (W15-10)", async () => {
    messagingClient = { downloadAttachment };
    const email = { id: "file:e1", kind: "document", title: "tuesday-pizza-class.md", createdAt: "1", taskId: "t9",
      ref: { roomId: "r1", attachmentId: "a1", contentType: "text/markdown" } };
    outputs.mockImplementation(async (f: { kind?: string }) => ({ outputs: f.kind === "document" ? [email] : [], next: null }));
    downloadAttachment.mockResolvedValue(new Blob(["# Pizza night\nThe kids help in the Kitchen."]));
    render(<CommandPalette open onOpenChange={() => {}} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "kitchen" } });
    expect(await screen.findByText("tuesday-pizza-class.md")).toBeInTheDocument();
    expect(downloadAttachment).toHaveBeenCalledWith("r1", "a1", { head: 8192 });
  });

  test("two letters ask nothing; three do", async () => {
    render(<CommandPalette open onOpenChange={() => {}} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "ba" } });
    await new Promise((r) => setTimeout(r, 300));
    expect(searchTasks).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "bak" } });
    await waitFor(() => expect(searchTasks).toHaveBeenCalledWith("bak", 6, expect.any(AbortSignal)));
  });
});

test("whole-word message hits come first (W15-10)", () => {
  const hits = [{ snippet: "on-line hello" }, { snippet: "Lindell called" }, { snippet: "the Lindell file" }];
  expect(wholeWordFirst(hits, "lindell").map((h) => h.snippet)).toEqual(["Lindell called", "the Lindell file", "on-line hello"]);
});
