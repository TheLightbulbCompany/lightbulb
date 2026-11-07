import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FIXTURES, SAM } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "@/components/tasks/__tests__/fakeTasks";

const live = vi.hoisted(() => ({ tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: SAM }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("../../workspaceController", () => ({ closePane: vi.fn(), setPaneFullScreen: vi.fn(), useWorkspaceState: () => ({ ready: true, fullScreen: false, pane: { panelId: "task-detail" } }) }));
vi.mock("@/components/messaging/SessionChat", () => ({ SessionChat: () => <section aria-label="Transcript" /> }));

import { TaskDockPanel } from "../TaskDockPanel";

let client: FakeTasksClient;
beforeEach(() => {
  const detail = FIXTURES.step08.details["t-signup"];
  client = fakeTasksClient([detail.task], { "t-signup": detail });
  live.tasks = client;
});

describe("TaskDockPanel", () => {

  it("mounts the thread pane", async () => {
    render(<Fresh><TaskDockPanel id="task-detail" params={{ taskId: "t-signup" }} /></Fresh>);
    expect(await screen.findByText("Workshop signup app")).toBeInTheDocument();
    expect(client.get).toHaveBeenCalledWith("t-signup");
    // The pane is a `.room-context` column, which messaging.css hides outside a `.room-context-host`: the dock
    // pane rendered blank on dev (U1). The host is what makes it show.
    expect(screen.getByRole("complementary", { name: "Task" }).parentElement).toHaveClass("room-context-host");
  });
});
