import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type QuestionView, type TaskDetail } from "@lightbulb/shared";
import { FIXTURES, PRIYA, SAM, answeredApprovalRow, answeredApprovalView, fixtureTask, viewAs } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown, messaging: { sendMessage: vi.fn(async () => ({})) } }));
const capture = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent }));
vi.mock("@/lib/analytics", () => ({ capture }));
// The transcript is the task room in the ordinary room column; a stand-in records which room it opens and whether it is read-only.
vi.mock("@/components/messaging/SessionChat", () => ({
  SessionChat: ({ sessionId, openChat, readOnly, hideMessage }: { sessionId: string; openChat: (id: string) => Promise<{ roomId: string }>; readOnly?: boolean; hideMessage?: (m: { kind?: string; taskRefs?: { qid?: string }[] }) => boolean }) => {
    const [room, setRoom] = useState("");
    useEffect(() => { void openChat(sessionId).then((r) => setRoom(r.roomId)); }, [sessionId, openChat]);
    return <section aria-label="Transcript" data-room={room} data-read-only={readOnly ? "yes" : "no"}
      data-hides={["question", "result"].filter((kind) => hideMessage?.({ kind })).join(",")}
      data-keeps-cancelled={hideMessage?.({ kind: "question", taskRefs: [{ qid: "q-gone" }] }) ? "no" : "yes"} />;
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
  it("section order: header, needs-you, transcript, details; the open card stands for the output", async () => {
    const waiting = { ...FIXTURES.step05.tasks[0], output: { idemKey: "e", kind: "app" as const, title: "Signup app preview" } };
    withDetail({ ...step8, task: waiting, questions: [openRow] }, [approval()]);
    pane();
    const status = await screen.findByRole("region", { name: "Request status" });
    const needs = screen.getByRole("group", { name: /provision a database/ });
    const transcript = screen.getByRole("region", { name: "Transcript" });
    expect(screen.queryByRole("region", { name: "Outputs" })).toBeNull();
    const details = screen.getByText("Details").closest("details")!;
    const order = [status, needs, transcript, details];
    for (let i = 1; i < order.length; i++) expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(details.open).toBe(false);
  });

  it("header controls per status", async () => {
    const { unmount } = pane();
    expect(await screen.findByRole("button", { name: "Stop" })).toBeInTheDocument();
    // Running: Stop (asked once), no Resolve under it.
    expect(screen.queryByRole("button", { name: "Close" })).toBeNull();
    // Where it runs is plumbing: Details only.
    expect(screen.getByText("Lightbulb cloud").closest("details")).not.toBeNull();
    expect(within(screen.getByRole("region", { name: "Request status" })).queryByText("Lightbulb cloud")).toBeNull();
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

  it("a cancelled question's card stays in Progress (it has no other place)", async () => {
    withDetail({ ...step8, questions: [{ ...openRow, qid: "q-gone", state: "cancelled" as const }] }, [approval({ id: "q-gone", state: "cancelled" })]);
    pane();
    const transcript = await screen.findByRole("region", { name: "Transcript" });
    await waitFor(() => expect(transcript).toHaveAttribute("data-keeps-cancelled", "yes"));
    expect(transcript).toHaveAttribute("data-hides", "question");
  });

  it("transcript is the task room, read-only; with no room composer on screen a one-line note steers it", async () => {
    live.messaging.sendMessage.mockClear();
    pane();
    const transcript = await screen.findByRole("region", { name: "Transcript" });
    await waitFor(() => expect(transcript).toHaveAttribute("data-room", step8.taskRoomId));
    expect(transcript).toHaveAttribute("data-read-only", "yes");
    // The question card shows once, at the top: its copy in the task room is left out of Progress.
    expect(transcript).toHaveAttribute("data-hides", "question");
    // One line, already there: no button that opens a second box.
    expect(screen.queryByRole("button", { name: "Add a note" })).toBeNull();
    await userEvent.type(screen.getByRole("textbox", { name: "Add a note" }), "Use the blue theme{Enter}");
    expect(live.messaging.sendMessage).toHaveBeenCalledWith(step8.taskRoomId, expect.objectContaining({ text: "Use the blue theme", mentions: [] }));
    expect(capture).toHaveBeenCalledWith("task_steer_sent", expect.objectContaining({ taskId: "t-signup", source: "note" }));
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
    await userEvent.click(screen.getByText("Steps"));
    await waitFor(() => expect(client.trace).toHaveBeenCalledWith("t-signup", 2));
    expect(client.trace).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("No steps yet.")).toBeInTheDocument();
  });

  it("cost hidden when nothing spent and no budget", async () => {
    withDetail({ ...step8, task: fixtureTask({ taskId: "t-x", title: "x" }) });
    pane(SAM, "t-x");
    await userEvent.click(await screen.findByText("Details"));
    expect(screen.queryByText("Cost")).toBeNull();
  });

  it("marks read once on open", async () => {
    pane();
    await screen.findByRole("region", { name: "Request status" });
    expect(client.markRead.mock.calls).toEqual([["t-signup"]]);
  });

  it("stop asks once; Keep going backs out; the word follows the returned summary", async () => {
    client.act.mockResolvedValueOnce({ ...step8.task, status: "resolved", statusReason: "stopped", shownGroup: "resolved", version: 9 });
    pane();
    await userEvent.click(await screen.findByRole("button", { name: "Stop" }));
    await userEvent.click(screen.getByRole("button", { name: "Keep going" }));
    expect(client.act).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    await userEvent.click(screen.getByRole("button", { name: "Stop it" }));
    expect(client.act).toHaveBeenCalledWith("t-signup", "stop", step8.task.version);
    expect(await screen.findByText("Closed")).toBeInTheDocument();
    // Stopped: the reason shows beside the word, and no "Done by" for work that was not done.
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(screen.queryByText(/Done by/)).toBeNull();
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
      "App publishMight have happened — check before retrying",
      "Pull request mergeDone",
    ]);
  });

  it("a failed load (not 403/404) offers Retry", async () => {
    client.get.mockRejectedValueOnce(new Error("503"));
    pane();
    expect(await screen.findByRole("alert")).toHaveTextContent("This request couldn't load.");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("region", { name: "Request status" })).toBeInTheDocument();
  });

  it("names no runtime until the task has one", async () => {
    withDetail({ ...step8, task: { ...step8.task, runsOn: null } });
    pane();
    await screen.findByRole("region", { name: "Request status" });
    expect(screen.queryByText("Lightbulb cloud")).toBeNull();
  });

  it("forbidden task reads as unavailable", async () => {
    client.get.mockRejectedValue(new ApiError(404, null));
    pane();
    expect(await screen.findByText("This request isn't available to you.")).toBeInTheDocument();
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
    const card = await screen.findByRole("group", { name: "Allow Engineering to send 'Join us Nov 20' to team@acme.com" });
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
    withDetail({ ...step8, task: FIXTURES.step07.tasks[1], questions: [{ ...openRow, qid: "q-email", kind: "review" }],
      effects: [{ idemKey: "e1", tool: "mail.send", state: "proposed", resultRef: null }] }, [review]);
    pane(SAM, "t-email");
    // The card and the effect read the Work row's word, not "Waiting for approval".
    const card = await screen.findByRole("group", { name: "Send the launch email?" });
    expect(within(card).getByText("Ready for review")).toBeInTheDocument();
    // The pending send is the card itself: no second row repeating it below.
    expect(screen.queryByRole("list", { name: "Effects" })).toBeNull();
    // The header follows the same state: ready for review, nothing running, so no Stop.
    const status = screen.getByRole("region", { name: "Request status" });
    // The whole line: actor, the status word once, no repeated "Ready for review".
    expect(status.firstElementChild).toHaveTextContent(/^Done by \w+, ready for your reviewNeeds you$/);
    expect(status).not.toHaveTextContent("is working on this");
    expect(within(status).queryByRole("button", { name: "Stop" })).toBeNull();
    await userEvent.click(await screen.findByRole("button", { name: "Send" }));
    expect(client.answer).toHaveBeenCalledWith("t-email", "q-email", expect.objectContaining({ optionId: "send" }));
    const calls = Object.values(client).filter((v) => typeof v === "function").flatMap((fn) => (fn as unknown as { mock: { calls: unknown[][] } }).mock.calls.flat());
    expect(JSON.stringify(calls)).not.toMatch(/apply/);
  });

  it("a Closed request's approval is read-only: no Allow once / Deny, and no Done by (dev walk N-F1)", async () => {
    const closed = { ...FIXTURES.step05.tasks[0], status: "resolved" as const, shownGroup: "resolved" as const };
    withDetail({ ...step8, task: closed, questions: [openRow] }, [approval()]);
    pane();
    const card = await screen.findByRole("group", { name: /provision a database/ });
    expect(within(card).getByText("Closed — not approved")).toBeInTheDocument();
    expect(within(card).queryByRole("button")).toBeNull();
    expect(screen.getByRole("region", { name: "Request status" })).not.toHaveTextContent("Done by");
  });

  it("Request changes asks what should change first, then sends it with the answer (dev walk N-F2)", async () => {
    const review = approval({ id: "q-email", kind: "review", text: "", title: "Send the launch email?",
      options: [{ id: "request_changes", label: "Request changes", style: "secondary" }, { id: "send", label: "Send", style: "primary" }] });
    withDetail({ ...step8, task: FIXTURES.step07.tasks[1], questions: [{ ...openRow, qid: "q-email", kind: "review" }] }, [review]);
    pane(SAM, "t-email");
    await userEvent.click(await screen.findByRole("button", { name: "Request changes" }));
    expect(client.answer).not.toHaveBeenCalled();
    const box = screen.getByRole("textbox", { name: "What should change?" });
    expect(screen.getByRole("button", { name: "Send changes" })).toBeDisabled();
    await userEvent.type(box, "Say the new date in the subject");
    await userEvent.click(screen.getByRole("button", { name: "Send changes" }));
    expect(client.answer).toHaveBeenCalledWith("t-email", "q-email", expect.objectContaining({ optionId: "request_changes", text: "Say the new date in the subject" }));
  });

  it("Cancel backs out of Request changes without answering", async () => {
    const review = approval({ id: "q-email", kind: "review", text: "", title: "Send the launch email?",
      options: [{ id: "request_changes", label: "Request changes", style: "secondary" }, { id: "send", label: "Send", style: "primary" }] });
    withDetail({ ...step8, task: FIXTURES.step07.tasks[1], questions: [{ ...openRow, qid: "q-email", kind: "review" }] }, [review]);
    pane(SAM, "t-email");
    await userEvent.click(await screen.findByRole("button", { name: "Request changes" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
    expect(client.answer).not.toHaveBeenCalled();
  });
});

describe("steering a finished request (dev walk N-F4)", () => {
  it("reads Working with Stop as soon as the note goes, before the revised result", async () => {
    const done = { ...step8.task, status: "idle" as const, shownGroup: "idle" as const, watching: false };
    withDetail({ ...step8, task: done, questions: [] }, []);
    pane();
    const status = await screen.findByRole("region", { name: "Request status" });
    expect(within(status).getByText("Done")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox", { name: "Add a note" }), "Make it one line{Enter}");
    await waitFor(() => expect(within(status).getByText("Working")).toBeInTheDocument());
    expect(within(status).getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });
});
