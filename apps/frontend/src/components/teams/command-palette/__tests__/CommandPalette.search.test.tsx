// W12-3: ⌘K finds tasks and what they made (server-filtered by `q`), opens each in its place, and names what it
// searches. Paperclip's Issues group stays out of super-agent workspaces.

import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const list = vi.fn();
const outputs = vi.fn();
const openTaskPane = vi.fn();
const openOutput = vi.fn();
let superAgent = true;

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/hooks/useLiveEvents", () => ({
  useSuperAgent: () => superAgent,
  useLiveEvents: () => ({ tasks: tasksClient }),
}));
const tasksClient = { list, outputs };
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

import { CommandPalette } from "../CommandPalette";

const doc = { id: "file:f1", kind: "document", title: "bakery.md", ref: {}, createdAt: "1", taskId: "t1" };

beforeEach(() => {
  superAgent = true;
  vi.clearAllMocks();
  list.mockResolvedValue({ tasks: [{ taskId: "t1", title: "Bakery taglines" }], next: null });
  outputs.mockResolvedValue({ outputs: [doc], next: null });
});

describe("command palette search (super-agent)", () => {
  test("finds tasks and files by name and opens each where it lives", async () => {
    render(<CommandPalette open onOpenChange={() => {}} />);
    const input = screen.getByPlaceholderText("Search projects, tasks, files and messages");
    fireEvent.change(input, { target: { value: "bakery" } });

    fireEvent.click(await screen.findByText("Bakery taglines"));
    expect(list).toHaveBeenCalledWith({ kind: "workspace" }, null, "bakery");
    expect(outputs).toHaveBeenCalledWith({ q: "bakery", limit: 10 });
    expect(openTaskPane).toHaveBeenCalledWith("t1");
    expect(screen.queryByText("bakery issue")).not.toBeInTheDocument();
  });

  test("a file opens in its viewer", async () => {
    render(<CommandPalette open onOpenChange={() => {}} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "bakery" } });
    fireEvent.click(await screen.findByText("bakery.md"));
    expect(openOutput).toHaveBeenCalledWith(doc);
  });

  test("without the super-agent nothing asks the task routes", async () => {
    superAgent = false;
    render(<CommandPalette open onOpenChange={() => {}} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "bakery" } });
    expect(await screen.findByText("bakery issue")).toBeInTheDocument();
    await waitFor(() => expect(list).not.toHaveBeenCalled());
  });
});
