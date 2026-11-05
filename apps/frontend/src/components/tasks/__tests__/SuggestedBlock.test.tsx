import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CanonicalRoomMessage, TaskDetail } from "@lightbulb/shared";
import { fixtureTask, SAM } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { MessageTasks, SuggestedBlock } from "../MessageTasks";

const p = (id: string) => fixtureTask({ taskId: id, title: `Task ${id}`, status: "proposed", shownGroup: null, proposalReason: "would use a paid tool" });
const a = p("a"), b = p("b");
let client: FakeTasksClient;
beforeEach(() => { client = fakeTasksClient([a, b]); live.tasks = client; });

describe("SuggestedBlock", () => {
  it("Start all only with two or more", async () => {
    const { unmount } = render(<Fresh><SuggestedBlock refs={[{ taskId: "a", role: "suggested" }]} taskById={new Map([["a", a]])} viewerId={SAM} /></Fresh>);
    expect(screen.getByRole("heading", { name: "Suggested" })).toBeInTheDocument();
    expect(screen.getByText("would use a paid tool")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start all" })).toBeNull();
    unmount();

    // The route lists only what started: b is absent, so it is refetched and keeps its card with an error.
    client.startMany.mockResolvedValue({ tasks: [{ ...a, status: "working", shownGroup: "working" }] } as never);
    render(<Fresh><SuggestedBlock refs={[{ taskId: "a", role: "suggested" }, { taskId: "b", role: "suggested" }]}
      taskById={new Map([["a", a], ["b", b]])} viewerId={SAM} /></Fresh>);
    await userEvent.click(screen.getByRole("button", { name: "Start all" }));
    expect(client.startMany).toHaveBeenCalledWith(["a", "b"]);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't start this one");
  });

  it("Start and Dismiss per card", async () => {
    render(<Fresh><SuggestedBlock refs={[{ taskId: "a", role: "suggested" }]} taskById={new Map([["a", a]])} viewerId={SAM} /></Fresh>);
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(client.act).toHaveBeenCalledWith("a", "dismiss", 1));
  });
});

describe("MessageTasks while the room list is not in", () => {
  it("placeholders while loading; a failed read offers Retry, never 'isn't available'", async () => {
    const message = { roomId: "r1", taskRefs: [{ taskId: "a", role: "started" }] } as unknown as CanonicalRoomMessage;
    client.list.mockRejectedValueOnce(new Error("503"));
    render(<Fresh><MessageTasks message={message} viewerId={SAM} /></Fresh>);
    expect(screen.getByLabelText("Loading requests")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("Requests couldn't load.");
    expect(screen.queryByText(/isn't available/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Task a")).toBeInTheDocument();
  });
});

describe("MessageTasks", () => {
  it("a card for a task the room list doesn't hold reads it by id, not 'isn't available'", async () => {
    const old = fixtureTask({ taskId: "old", title: "Last month's report", status: "idle", shownGroup: "idle" });
    client = fakeTasksClient([a], { old: { task: old, attempts: [], questions: [], effects: [], subscriptions: [], taskRoomId: "tr" } as TaskDetail });
    live.tasks = client;
    const message = { roomId: "r1", taskRefs: [{ taskId: "old", role: "started" }] } as unknown as CanonicalRoomMessage;
    render(<Fresh><MessageTasks message={message} viewerId={SAM} /></Fresh>);
    expect(await screen.findByText("Last month's report")).toBeInTheDocument();
    expect(client.get).toHaveBeenCalledWith("old");
    expect(screen.queryByText(/isn't available/)).toBeNull();
  });
});
