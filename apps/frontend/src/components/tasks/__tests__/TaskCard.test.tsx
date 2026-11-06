import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@lightbulb/shared";
import { FIXTURES, PRIYA, SAM, viewAs } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/components/canvas/RequestApp", () => ({ RequestApp: () => null })); // its own test: RequestApp.test.tsx

import { TaskCard } from "../TaskCard";

const byId = (step: keyof typeof FIXTURES, id: string) => FIXTURES[step].tasks.find((t) => t.taskId === id)!;

/** A question as the viewer's view returns it (the decision card's input). */
const qv = (over: Record<string, unknown> = {}) => ({
  id: "q-provision", taskId: "t-signup", kind: "approval", subkind: null, title: "Provision database and Stripe", text: "",
  options: [{ id: "allow_once", label: "Allow once", style: "primary" }, { id: "deny", label: "Deny", style: "danger" }],
  state: "open", answerers: [{ id: SAM, name: "Sam" }], canAnswer: true, waitingLabel: null,
  answer: null, answeredBy: null, answeredAt: null, createdAt: "2026-11-04T14:00:00Z", version: 7, ...over,
});
const REVIEW = { id: "q-email", taskId: "t-email", kind: "review", title: "Send the launch email?", version: 2,
  options: [{ id: "send", label: "Send", style: "primary" }, { id: "request_changes", label: "Request changes", style: "secondary" }] };
let client: FakeTasksClient;
beforeEach(() => {
  client = fakeTasksClient(FIXTURES.step05.tasks);
  live.tasks = client;
});

describe("TaskCard", () => {
  it("one chip per thing it made, two then +N more (which opens the task); no 📎 line", async () => {
    const onOpen = vi.fn();
    const outputs = ["Brief.md", "Plan.md", "Notes.md"].map((title, i) => ({ id: `f${i}`, kind: "document" as const, title, ref: {}, createdAt: "2026-11-06T10:00:00Z" }));
    render(<Fresh><TaskCard task={{ ...byId("step02", "t-signup"), status: "resolved", shownGroup: "resolved", outputs }} taskId="t-signup" viewerId={SAM} onOpen={onOpen} /></Fresh>);
    const chips = screen.getByRole("list", { name: "Made by this task" });
    expect(within(chips).getByRole("button", { name: "Open Brief · MD" })).toBeInTheDocument();
    expect(within(chips).queryByText("Notes.md")).toBeNull();
    await userEvent.click(within(chips).getByRole("button", { name: "+1 more" }));
    expect(onOpen).toHaveBeenCalledWith("t-signup");
    expect(screen.queryByText(/📎/)).toBeNull();
  });

  it("a resolved card in the history offers no Reopen (the pane and overview do)", () => {
    render(<Fresh><TaskCard task={{ ...byId("step02", "t-signup"), status: "resolved", shownGroup: "resolved" }} taskId="t-signup" viewerId={SAM} /></Fresh>);
    expect(screen.getByText("Closed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reopen" })).toBeNull();
  });

  it("names a specialist that is an agent id from the agents list", () => {
    const id = "5b36216c-196c-48af-ad2a-6817c42a1de3";
    (live as { agentNames?: Map<string, string> }).agentNames = new Map([[id, "Finance"]]);
    try {
      render(<Fresh><TaskCard task={{ ...byId("step02", "t-signup"), specialist: id }} taskId="t-signup" viewerId={SAM} /></Fresh>);
      expect(screen.getByText("Finance")).toBeInTheDocument();
      expect(screen.queryByText(new RegExp(id))).toBeNull();
    } finally { delete (live as { agentNames?: unknown }).agentNames; }
  });

  it("word, meta and action per status and viewer", async () => {
    const { unmount } = render(<Fresh><TaskCard task={byId("step02", "t-signup")} taskId="t-signup" viewerId={SAM} /></Fresh>);
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByText("Engineering")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Allow once" })).toBeNull();
    unmount();

    const waiting = byId("step05", "t-signup");
    client.questions.mockResolvedValue({ items: [qv()] } as never);
    const sam = render(<Fresh><TaskCard task={waiting} taskId="t-signup" viewerId={SAM} /></Fresh>);
    expect(screen.getByText("Needs you")).toBeInTheDocument();
    // The one decision card on the card itself: the question, then Deny / Allow once (UX slice 2); a Worker's own ask
    // (no preview) can't be remembered, so no Always allow (W12-9).
    const card = await screen.findByRole("group", { name: "Provision database and Stripe" });
    expect(within(card).getAllByRole("button").map((b) => b.textContent)).toEqual(["Deny", "Allow once"]);
    expect(screen.queryByText("Needs approval: Provision database and Stripe")).toBeNull(); // said once, by the card
    sam.unmount();

    render(<Fresh><TaskCard task={viewAs(waiting, PRIYA)} taskId="t-signup" viewerId={PRIYA} /></Fresh>);
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByText("Waiting on Sam")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Allow once" })).toBeNull();
  });

  it("whole card opens, inner button does not", async () => {
    const onOpen = vi.fn();
    client.questions.mockResolvedValue({ items: [qv()] } as never);
    render(<Fresh><TaskCard task={byId("step05", "t-signup")} taskId="t-signup" viewerId={SAM} onOpen={onOpen} /></Fresh>);
    await userEvent.click(screen.getByRole("button", { name: "Open Workshop signup app, Needs you" }));
    expect(onOpen).toHaveBeenCalledWith("t-signup");
    onOpen.mockClear();
    await userEvent.click(await screen.findByRole("button", { name: "Allow once" }));
    await waitFor(() => expect(client.answer).toHaveBeenCalledWith("t-signup", "q-provision",
      expect.objectContaining({ optionId: "allow_once", expectVersion: 7, clientAnswerId: expect.any(String) })));
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("a review card shows To and Subject before its one-press Send, which waits for them", async () => {
    const email = FIXTURES.step07.tasks.find((t) => t.taskId === "t-email")!;
    let resolve!: (v: unknown) => void;
    client.questions.mockReturnValue(new Promise((r) => { resolve = r; }) as never);
    render(<Fresh><TaskCard task={email} taskId="t-email" viewerId={SAM} /></Fresh>);
    // Nothing to press until the card can show what it sends.
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    resolve({ items: [qv({ ...REVIEW, preview: { kind: "mail.send", to: "team@acme.com", subject: "Join us Nov 20" } })] });
    expect(await screen.findByText("team@acme.com")).toBeInTheDocument();
    expect(screen.getByText("Join us Nov 20")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });

  it("details that fail to load: the card says so and offers no blind answer", async () => {
    const onOpen = vi.fn();
    client.questions.mockRejectedValue(new Error("503"));
    render(<Fresh><TaskCard task={byId("step05", "t-signup")} taskId="t-signup" viewerId={SAM} onOpen={onOpen} /></Fresh>);
    expect(await screen.findByText("The details didn't load. Open the task to decide.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Allow once" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Open Workshop signup app, Needs you" }));
    expect(onOpen).toHaveBeenCalledWith("t-signup");
    expect(client.answer).not.toHaveBeenCalled();
  });

  it("stale version 409 refetches, no second POST", async () => {
    const fees = byId("step03", "t-fees");
    const failed = { ...byId("step02", "t-fees"), status: "failed" as const, shownGroup: "waiting_on_you" as const };
    client.act.mockRejectedValueOnce(new ApiError(409, null));
    client.get.mockResolvedValue({ task: fees } as never);
    render(<Fresh><TaskCard task={failed} taskId="t-fees" viewerId={SAM} /></Fresh>);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Someone else got to this first");
    expect(client.act).toHaveBeenCalledTimes(1);
  });

  it("missing task renders unavailable row", () => {
    render(<Fresh><TaskCard task={undefined} taskId="gone" viewerId={SAM} /><TaskCard task={byId("step02", "t-email")} taskId="t-email" viewerId={SAM} /></Fresh>);
    expect(screen.getByText("This task isn't available")).toBeInTheDocument();
    expect(screen.getByText("Launch email draft")).toBeInTheDocument();
  });

  it("review card's next step is the answer (no /apply)", async () => {
    client.questions.mockResolvedValue({ items: [qv(REVIEW)] } as never);
    render(<Fresh><TaskCard task={byId("step07", "t-email")} taskId="t-email" viewerId={SAM} /></Fresh>);
    expect(screen.getByText(/Join us Nov 20/)).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Send" }));
    await waitFor(() => expect(client.answer).toHaveBeenCalledWith("t-email", "q-email", expect.objectContaining({ optionId: "send" })));
  });
});
