import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@lightbulb/shared";
import { FIXTURES, PRIYA, SAM, viewAs } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { TaskCard } from "../TaskCard";

const byId = (step: keyof typeof FIXTURES, id: string) => FIXTURES[step].tasks.find((t) => t.taskId === id)!;
let client: FakeTasksClient;
beforeEach(() => {
  client = fakeTasksClient(FIXTURES.step05.tasks);
  live.tasks = client;
});

describe("TaskCard", () => {
  it("a resolved card in the history offers no Reopen (the pane and overview do)", () => {
    render(<Fresh><TaskCard task={{ ...byId("step02", "t-signup"), status: "resolved", shownGroup: "resolved" }} taskId="t-signup" viewerId={SAM} /></Fresh>);
    expect(screen.getByText("Resolved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reopen" })).toBeNull();
  });

  it("names a specialist that is an agent id from the agents list", () => {
    const id = "5b36216c-196c-48af-ad2a-6817c42a1de3";
    (live as { agentNames?: Map<string, string> }).agentNames = new Map([[id, "Finance"]]);
    try {
      render(<Fresh><TaskCard task={{ ...byId("step02", "t-signup"), specialist: id }} taskId="t-signup" viewerId={SAM} /></Fresh>);
      expect(screen.getByText("Lightbulb · Finance")).toBeInTheDocument();
      expect(screen.queryByText(new RegExp(id))).toBeNull();
    } finally { delete (live as { agentNames?: unknown }).agentNames; }
  });

  it("word, meta and action per status and viewer", () => {
    const { unmount } = render(<Fresh><TaskCard task={byId("step02", "t-signup")} taskId="t-signup" viewerId={SAM} /></Fresh>);
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByText("Lightbulb · Engineering")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    unmount();

    const waiting = byId("step05", "t-signup");
    const sam = render(<Fresh><TaskCard task={waiting} taskId="t-signup" viewerId={SAM} /></Fresh>);
    expect(screen.getByText("Waiting on you")).toBeInTheDocument();
    expect(screen.getByText("Needs approval: Provision database and Stripe")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    sam.unmount();

    render(<Fresh><TaskCard task={viewAs(waiting, PRIYA)} taskId="t-signup" viewerId={PRIYA} /></Fresh>);
    expect(screen.getByText("Waiting on Sam")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("whole card opens, inner button does not", async () => {
    const onOpen = vi.fn();
    client.questions.mockResolvedValue({ items: [{ id: "q-provision", version: 7 }] } as never);
    render(<Fresh><TaskCard task={byId("step05", "t-signup")} taskId="t-signup" viewerId={SAM} onOpen={onOpen} /></Fresh>);
    await userEvent.click(screen.getByRole("button", { name: "Open Workshop signup app, Waiting on you" }));
    expect(onOpen).toHaveBeenCalledWith("t-signup");
    onOpen.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(client.answer).toHaveBeenCalledWith("t-signup", "q-provision",
      expect.objectContaining({ optionId: "allow_once", expectVersion: 7, clientAnswerId: expect.any(String) })));
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("a review card shows To and Subject before its one-press Send, which waits for them", async () => {
    const email = FIXTURES.step07.tasks.find((t) => t.taskId === "t-email")!;
    let resolve!: (v: unknown) => void;
    client.questions.mockReturnValue(new Promise((r) => { resolve = r; }) as never);
    render(<Fresh><TaskCard task={email} taskId="t-email" viewerId={SAM} /></Fresh>);
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    resolve({ items: [{ id: "q-email", version: 2, preview: { kind: "mail.send", to: "team@acme.com", subject: "Join us Nov 20" } }] });
    expect(await screen.findByText("team@acme.com")).toBeInTheDocument();
    expect(screen.getByText("Join us Nov 20")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeEnabled());
  });

  it("details that fail to load: the card says so, and its button opens the thread instead of acting blind", async () => {
    const onOpen = vi.fn();
    client.questions.mockRejectedValue(new Error("503"));
    render(<Fresh><TaskCard task={byId("step05", "t-signup")} taskId="t-signup" viewerId={SAM} onOpen={onOpen} /></Fresh>);
    expect(await screen.findByText("The details didn't load. Open the thread to decide.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onOpen).toHaveBeenCalledWith("t-signup");
    expect(client.answer).not.toHaveBeenCalled();
  });

  it("stale version 409 refetches, no second POST", async () => {
    const fees = byId("step03", "t-fees");
    const proposed = { ...byId("step02", "t-fees") };
    client.act.mockRejectedValueOnce(new ApiError(409, null));
    client.get.mockResolvedValue({ task: fees } as never);
    render(<Fresh><TaskCard task={{ ...proposed, status: "failed", shownGroup: "waiting_on_you" }} taskId="t-fees" viewerId={SAM} /></Fresh>);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Someone else got to this first");
    expect(client.act).toHaveBeenCalledTimes(1);
  });

  it("missing task renders unavailable row", () => {
    render(<Fresh><TaskCard task={undefined} taskId="gone" viewerId={SAM} /><TaskCard task={byId("step02", "t-email")} taskId="t-email" viewerId={SAM} /></Fresh>);
    expect(screen.getByText("This thread isn't available")).toBeInTheDocument();
    expect(screen.getByText("Launch email draft")).toBeInTheDocument();
  });

  it("review card's next step is the answer (no /apply)", async () => {
    client.questions.mockResolvedValue({ items: [{ id: "q-email", version: 2 }] } as never);
    render(<Fresh><TaskCard task={byId("step07", "t-email")} taskId="t-email" viewerId={SAM} /></Fresh>);
    expect(screen.getByText(/Join us Nov 20/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(client.answer).toHaveBeenCalledWith("t-email", "q-email", expect.objectContaining({ optionId: "send" })));
  });
});
