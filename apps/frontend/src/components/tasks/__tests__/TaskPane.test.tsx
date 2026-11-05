import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type QuestionView, type TaskDetail } from "@lightbulb/shared";
import { FIXTURES, PRIYA, SAM, answeredApprovalRow, answeredApprovalView, fixtureTask, viewAs } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown }));
const capture = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent }));
vi.mock("@/lib/analytics", () => ({ capture }));
// The transcript is the task room in the ordinary room column; a stand-in records which room it opens.
vi.mock("@/components/messaging/SessionChat", () => ({
  SessionChat: ({ sessionId, openChat, words, onSend }: {
    sessionId: string; openChat: (id: string) => Promise<{ roomId: string }>; words: { compose?: string }; onSend?: () => void;
  }) => {
    const [room, setRoom] = useState("");
    useEffect(() => { void openChat(sessionId).then((r) => setRoom(r.roomId)); }, [sessionId, openChat]);
    return <section aria-label="Transcript" data-room={room}><textarea placeholder={words.compose} /><button onClick={onSend}>Send steer</button></section>;
  },
}));

import { TaskPane } from "../TaskPane";

const approval = (over: Partial<QuestionView> = {}): QuestionView => ({
  id: "q-provision", taskId: "t-signup", kind: "approval", subkind: null, title: "Provision database and Stripe",
  text: "Allow Lightbulb · Engineering to provision a database and Stripe payments?",
  options: [{ id: "allow_once", label: "Allow once", style: "primary" }, { id: "deny", label: "Deny", style: "danger" }],
  state: "open", answerers: [{ id: SAM, name: "Sam" }], canAnswer: true, waitingLabel: "Waiting on you",
  answer: null, answeredBy: null, answeredAt: null, createdAt: "2026-11-04T14:00:00Z", version: 4, ...over,
});

let client: FakeTasksClient;
const step8 = FIXTURES.step08.details["t-signup"];
/** The detail (rows) and, beside it, what Q's `/questions` route answers for this viewer. */
function withDetail(detail: TaskDetail, views: QuestionView[] = detail.questions.length ? [answeredApprovalView] : []) {
  client = fakeTasksClient([detail.task], { [detail.task.taskId]: detail });
  client.questions.mockResolvedValue({ items: views } as never);
  live.tasks = client;
}
const openRow = { ...answeredApprovalRow, state: "open" as const, answer: null, answeredBy: null, answeredAt: null, version: 4 };
const pane = (viewer = SAM, taskId = "t-signup") => render(<Fresh><TaskPane taskId={taskId} viewerId={viewer} onClose={vi.fn()} /></Fresh>);

beforeEach(() => { capture.mockClear(); withDetail(step8); });

describe("TaskPane", () => {
  it("section order: header, needs-you, transcript, outputs, details", async () => {
    const waiting = { ...FIXTURES.step05.tasks[0], output: { idemKey: "e", kind: "app" as const, title: "Signup app preview" } };
    withDetail({ ...step8, task: waiting, questions: [openRow] }, [approval()]);
    pane();
    const status = await screen.findByRole("region", { name: "Thread status" });
    const needs = screen.getByRole("group", { name: /provision a database/ });
    const transcript = screen.getByRole("region", { name: "Transcript" });
    const outputs = screen.getByRole("region", { name: "Outputs" });
    const details = screen.getByText("Details").closest("details")!;
    const order = [status, needs, transcript, outputs, details];
    for (let i = 1; i < order.length; i++) expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(details.open).toBe(false);
  });

  it("header controls per status", async () => {
    const { unmount } = pane();
    expect(await screen.findByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resolve" })).toBeInTheDocument();
    expect(screen.getByText("Lightbulb cloud")).toBeInTheDocument();
    unmount();
    withDetail({ ...step8, task: { ...step8.task, status: "resolved", shownGroup: "resolved" } });
    pane();
    expect(await screen.findByRole("button", { name: "Reopen" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  });

  it("non-answerer sees Waiting on Sam without buttons", async () => {
    const waiting = FIXTURES.step05.tasks[0];
    withDetail({ ...step8, task: viewAs(waiting, PRIYA), questions: [openRow] }, [approval({ canAnswer: false, waitingLabel: "Waiting on Sam" })]);
    const { unmount } = pane(PRIYA);
    const card = await screen.findByRole("group", { name: /provision a database/ });
    expect(card).toHaveTextContent("Waiting on Sam");
    expect(within(card).queryByRole("button")).toBeNull();
    unmount();

    withDetail({ ...step8, task: waiting, questions: [openRow] }, [approval()]);
    pane(SAM);
    const mine = await screen.findByRole("group", { name: "Allow Lightbulb · Engineering to provision a database and Stripe payments?" });
    expect(within(mine).getAllByRole("button").map((b) => b.textContent)).toEqual(["Allow once", "Deny"]);
  });

  it("transcript is the task room; steer fires the event", async () => {
    pane();
    const transcript = await screen.findByRole("region", { name: "Transcript" });
    await waitFor(() => expect(transcript).toHaveAttribute("data-room", step8.taskRoomId));
    expect(screen.getByPlaceholderText("Steer this thread…")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Send steer" }));
    expect(capture).toHaveBeenCalledWith("task_steer_sent", expect.objectContaining({ taskId: "t-signup" }));
  });

  it("answered approval leaves a line", async () => {
    pane();
    expect(await screen.findByText(/^Approved by Sam · /)).toBeInTheDocument();
  });

  it("details folded, trace on demand, cost line", async () => {
    pane();
    await screen.findByText("Details");
    expect(client.trace).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText("Details"));
    expect(screen.getByText("$0.84 of $5 budget")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Trace"));
    await waitFor(() => expect(client.trace).toHaveBeenCalledWith("t-signup", 2));
    expect(client.trace).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("No tool calls yet.")).toBeInTheDocument();
  });

  it("cost hidden when nothing spent and no budget", async () => {
    withDetail({ ...step8, task: fixtureTask({ taskId: "t-x", title: "x" }) });
    pane(SAM, "t-x");
    await userEvent.click(await screen.findByText("Details"));
    expect(screen.queryByText("Cost")).toBeNull();
  });

  it("marks read once on open", async () => {
    pane();
    await screen.findByRole("region", { name: "Thread status" });
    expect(client.markRead.mock.calls).toEqual([["t-signup"]]);
  });

  it("stop is one press; the word follows the returned summary", async () => {
    client.act.mockResolvedValueOnce({ ...step8.task, status: "resolved", statusReason: "stopped", shownGroup: "resolved", version: 9 });
    pane();
    await userEvent.click(await screen.findByRole("button", { name: "Stop" }));
    expect(client.act).toHaveBeenCalledWith("t-signup", "stop", step8.task.version);
    expect(await screen.findByText("Stopped")).toBeInTheDocument();
  });

  it("effects: a refused landing did not happen; unknown says to check first", async () => {
    withDetail({ ...step8, effects: [
      { idemKey: "e1", tool: "mail.send", state: "done", output: { kind: "mail", title: "Launch email" }, resultRef: "refused:rate_limited" },
      { idemKey: "e2", tool: "app.publish", state: "unknown", resultRef: null },
      { idemKey: "e3", tool: "pr.merge", state: "done", resultRef: "gh://pr/12" },
    ] });
    pane();
    const effects = await screen.findByRole("list", { name: "Effects" });
    expect(within(effects).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Launch emailDidn't happen: rate limited",
      "app.publishMight have happened — check before retrying",
      "pr.mergeDone",
    ]);
  });

  it("forbidden task reads as unavailable", async () => {
    client.get.mockRejectedValue(new ApiError(404, null));
    pane();
    expect(await screen.findByText("This thread isn't available to you.")).toBeInTheDocument();
  });
});

describe("NeedsYouCard", () => {
  beforeEach(() => withDetail({ ...step8, task: FIXTURES.step05.tasks[0], questions: [openRow] }, [approval()]));

  it("approve posts optionId with the question's version; buttons go", async () => {
    client.answer.mockResolvedValueOnce({ question: approval({ state: "answered" }), next: FIXTURES.step07.tasks[0] } as never);
    pane();
    await userEvent.click(await screen.findByRole("button", { name: "Allow once" }));
    expect(client.answer).toHaveBeenCalledWith("t-signup", "q-provision",
      { optionId: "allow_once", clientAnswerId: expect.stringMatching(/^[0-9a-f-]{36}$/), expectVersion: 4 });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Allow once" })).toBeNull());
  });

  it("409 shows who answered and removes the buttons, with no second POST", async () => {
    // Priya answered between Sam's read and his press: the answer route says not_open, the
    // refetched question is answered, and the card gives way to the decision line.
    const byPriya = approval({ state: "answered", canAnswer: false, answeredBy: { id: PRIYA, name: "Priya" }, answeredAt: "2026-11-04T14:03:00Z", answer: { optionId: "allow_once", text: null } });
    client.questions.mockResolvedValueOnce({ items: [approval()] } as never).mockResolvedValue({ items: [byPriya] } as never);
    client.answer.mockRejectedValueOnce(new ApiError(409, { code: "not_open" }));
    pane();
    await userEvent.click(await screen.findByRole("button", { name: "Allow once" }));
    expect(await screen.findByText(/^Approved by Priya · /)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Allow once" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Deny" })).toBeNull();
    expect(client.answer).toHaveBeenCalledTimes(1);
  });

  it("403 copy", async () => {
    client.answer.mockRejectedValueOnce(new ApiError(403, null));
    pane();
    await userEvent.click(await screen.findByRole("button", { name: "Allow once" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Only Sam can approve this");
  });

  it("shows the exact effect before the approver decides", async () => {
    withDetail({ ...step8, task: FIXTURES.step05.tasks[0], questions: [openRow] }, [approval({
      title: "send 'Join us Nov 20' to team@acme.com", text: "",
      preview: { kind: "mail.send", to: "team@acme.com", subject: "Join us Nov 20", body: "Hi all, signups open today.", idemKey: "x" },
    })]);
    pane();
    const card = await screen.findByRole("group", { name: "Allow Lightbulb · Engineering to send 'Join us Nov 20' to team@acme.com" });
    expect(within(card).getByLabelText("What this does")).toBeInTheDocument();
    expect(card).toHaveTextContent("Toteam@acme.com");
    expect(card).toHaveTextContent("MessageHi all, signups open today.");
    expect(card).not.toHaveTextContent("idemKey");
  });

  it("a free-text question (no options) takes a typed answer, posted as optionId answer", async () => {
    const ask = approval({ kind: "question", title: "Which date should the launch use?", text: "", options: [] });
    withDetail({ ...step8, task: FIXTURES.step05.tasks[0], questions: [{ ...openRow, kind: "question", options: [] }] }, [ask]);
    pane();
    const box = await screen.findByRole("textbox", { name: "Your answer" });
    expect(screen.getByRole("button", { name: "Send answer" })).toBeDisabled();
    await userEvent.type(box, "Nov 20");
    await userEvent.click(screen.getByRole("button", { name: "Send answer" }));
    expect(client.answer).toHaveBeenCalledWith("t-signup", "q-provision", expect.objectContaining({ optionId: "answer", text: "Nov 20", expectVersion: 4 }));
  });

  it("review answer is the apply (no /apply)", async () => {
    const review = approval({ id: "q-email", kind: "review", text: "", title: "Send the launch email?",
      options: [{ id: "request_changes", label: "Request changes", style: "secondary" }, { id: "send", label: "Send", style: "primary" }] });
    withDetail({ ...step8, task: FIXTURES.step07.tasks[1], questions: [{ ...openRow, qid: "q-email", kind: "review" }] }, [review]);
    pane(SAM, "t-email");
    await userEvent.click(await screen.findByRole("button", { name: "Send" }));
    expect(client.answer).toHaveBeenCalledWith("t-email", "q-email", expect.objectContaining({ optionId: "send" }));
    const calls = Object.values(client).filter((v) => typeof v === "function").flatMap((fn) => (fn as unknown as { mock: { calls: unknown[][] } }).mock.calls.flat());
    expect(JSON.stringify(calls)).not.toMatch(/apply/);
  });
});
