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
  SessionChat: ({ sessionId, openChat, readOnly, hideMessage }: { sessionId: string; openChat: (id: string) => Promise<{ roomId: string }>; readOnly?: boolean; hideMessage?: (m: { kind?: string; taskRefs?: { qid?: string; taskId?: string }[] }) => boolean }) => {
    const [room, setRoom] = useState("");
    useEffect(() => { void openChat(sessionId).then((r) => setRoom(r.roomId)); }, [sessionId, openChat]);
    return <section aria-label="Transcript" data-room={room} data-read-only={readOnly ? "yes" : "no"}
      data-hides={["question", "result"].filter((kind) => hideMessage?.({ kind })).join(",")}
      data-keeps-cancelled={hideMessage?.({ kind: "question", taskRefs: [{ qid: "q-gone" }] }) ? "no" : "yes"}
      data-hides-own-card={hideMessage?.({ kind: "message", taskRefs: [{ taskId: "t-signup" }] }) ? "yes" : "no"}
      data-hides-other-card={hideMessage?.({ kind: "message", taskRefs: [{ taskId: "t-other" }] }) ? "yes" : "no"} />;
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
    const status = await screen.findByRole("region", { name: "Task status" });
    const needs = screen.getByRole("group", { name: /Provision database and Stripe/ });
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
    expect(within(screen.getByRole("region", { name: "Task status" })).queryByText("Lightbulb cloud")).toBeNull();
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
    const card = await screen.findByRole("group", { name: /Provision database and Stripe/ });
    expect(card).toHaveTextContent("Waiting on Sam");
    expect(within(card).queryByRole("button")).toBeNull();
    unmount();

    withDetail({ ...step8, task: waiting, questions: [openRow] }, [approval()]);
    pane(SAM);
    const mine = await screen.findByRole("group", { name: "Provision database and Stripe" });
    // The one card's verbs, safe first; this ask names no fixed target, so Always allow shows disabled with why.
    expect(within(mine).getAllByRole("button").map((b) => b.textContent)).toEqual(["Deny", "Allow once", "Always allow"]);
    expect(within(mine).getByRole("button", { name: "Always allow" })).toBeDisabled();
    expect(within(mine).getByRole("button", { name: "Always allow" })).toHaveAccessibleDescription(/Can't be remembered/);
    expect(mine).toHaveTextContent(/^Engineering · \d+[smhd] ago/);
  });

  it("W7-4: a closed purchase leaves no card in Progress; Decisions says it closed unapproved and the footer says Not approved", async () => {
    const closed = { ...step8, task: { ...step8.task, status: "resolved" as const, shownGroup: "resolved" as const },
      questions: [{ ...openRow, qid: "q-gone", state: "cancelled" as const }],
      effects: [{ idemKey: "e1", tool: "purchase", kind: "purchase", state: "proposed" }] };
    withDetail(closed, [approval({ id: "q-gone", state: "cancelled" })]);
    pane();
    const transcript = await screen.findByRole("region", { name: "Transcript" });
    // The cancelled ask's card and the request's own card are left out (they read as an empty card); others stay.
    await waitFor(() => expect(transcript).toHaveAttribute("data-keeps-cancelled", "no"));
    expect(transcript).toHaveAttribute("data-hides-own-card", "yes");
    expect(transcript).toHaveAttribute("data-hides-other-card", "no");
    expect(within(screen.getByRole("list", { name: "Decisions" })).getByText(/^Closed — not approved: /)).toBeInTheDocument();
    const effects = screen.getByRole("list", { name: "Effects" });
    expect(effects).toHaveTextContent("Not approved");
    expect(effects).not.toHaveTextContent("Waiting for approval");
  });

  it("W8-4: after Reopen the old Closed line hides under the new live approval; a second Close reads once, one footer row", async () => {
    const purchase = (state: "proposed", idemKey: string) => ({ idemKey, tool: "purchase", kind: "purchase", state });
    // Reopened: the first ask was closed, the new one is live.
    withDetail({ ...step8, task: FIXTURES.step05.tasks[0], questions: [{ ...openRow, qid: "q-1", state: "cancelled" as const }, { ...openRow, qid: "q-2" }],
      effects: [purchase("proposed", "e1"), purchase("proposed", "e2")] },
    [approval({ id: "q-1", state: "cancelled", createdAt: "2026-11-05T22:08:00Z" }), approval({ id: "q-2", createdAt: "2026-11-05T22:11:00Z" })]);
    const { unmount } = pane();
    await screen.findByRole("group", { name: /Provision database and Stripe/ });
    expect(screen.queryByRole("list", { name: "Decisions" })).toBeNull();
    unmount();
    // Closed again: two cancelled asks for the same thing, two unapproved proposals.
    withDetail({ ...step8, task: { ...step8.task, status: "resolved" as const, shownGroup: "resolved" as const },
      questions: [{ ...openRow, qid: "q-1", state: "cancelled" as const }, { ...openRow, qid: "q-2", state: "cancelled" as const }],
      effects: [purchase("proposed", "e1"), purchase("proposed", "e2")] },
    [approval({ id: "q-1", state: "cancelled" }), approval({ id: "q-2", state: "cancelled" })]);
    pane();
    const decisions = await screen.findByRole("list", { name: "Decisions" });
    expect(within(decisions).getAllByRole("listitem")).toHaveLength(1);
    expect(within(screen.getByRole("list", { name: "Effects" })).getAllByRole("listitem")).toHaveLength(1);
  });

  it("transcript is the task room, read-only; the pane's own line steers it (the room composer is never taken over)", async () => {
    live.messaging.sendMessage.mockClear();
    pane();
    const transcript = await screen.findByRole("region", { name: "Transcript" });
    await waitFor(() => expect(transcript).toHaveAttribute("data-room", step8.taskRoomId));
    expect(transcript).toHaveAttribute("data-read-only", "yes");
    // The question card shows once, at the top: its copy in the task room is left out of Progress.
    expect(transcript).toHaveAttribute("data-hides", "question");
    // One line, already there: no button that opens a second box.
    expect(screen.queryByRole("button", { name: "Steer this task" })).toBeNull();
    await userEvent.type(screen.getByRole("textbox", { name: "Steer this task" }), "Use the blue theme{Enter}");
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
    // Steps sit under Progress, not inside Details (UX slice 3).
    await userEvent.click(screen.getByText(/^Steps/));
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
    await screen.findByRole("region", { name: "Task status" });
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
    expect(await screen.findByRole("alert")).toHaveTextContent("This task couldn't load.");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("region", { name: "Task status" })).toBeInTheDocument();
  });

  it("names no runtime until the task has one", async () => {
    withDetail({ ...step8, task: { ...step8.task, runsOn: null } });
    pane();
    await screen.findByRole("region", { name: "Task status" });
    expect(screen.queryByText("Lightbulb cloud")).toBeNull();
  });

  it("forbidden task reads as unavailable", async () => {
    client.get.mockRejectedValue(new ApiError(404, null));
    pane();
    expect(await screen.findByText("This task isn't available to you.")).toBeInTheDocument();
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
    // The card carries no status word (the card itself means "needs you"): who asks, then the question.
    const card = await screen.findByRole("group", { name: "Send the launch email?" });
    expect(card).not.toHaveTextContent("Ready for review");
    // Safe first: Request changes before Send.
    expect(within(card).getAllByRole("button").map((b) => b.textContent)).toEqual(["Request changes", "Send"]);
    // The pending send is the card itself: no second row repeating it below.
    expect(screen.queryByRole("list", { name: "Effects" })).toBeNull();
    // The header follows the same state: ready for review, nothing running, so no Stop.
    const status = screen.getByRole("region", { name: "Task status" });
    // The whole line: the task's title and the reason once; the status word sits once, in the header row.
    expect(status.firstElementChild).toHaveTextContent(/Ready for review$/);
    expect(status.firstElementChild).not.toHaveTextContent(/Needs you|Done by|ready for your review/);
    expect(screen.getByRole("banner").querySelector(".pane-header__meta")).toHaveTextContent(/Needs you$/);
    expect(status).not.toHaveTextContent("is working on this");
    expect(within(screen.getByRole("banner")).queryByRole("button", { name: "Stop" })).toBeNull();
    await userEvent.click(await screen.findByRole("button", { name: "Send" }));
    expect(client.answer).toHaveBeenCalledWith("t-email", "q-email", expect.objectContaining({ optionId: "send" }));
    const calls = Object.values(client).filter((v) => typeof v === "function").flatMap((fn) => (fn as unknown as { mock: { calls: unknown[][] } }).mock.calls.flat());
    expect(JSON.stringify(calls)).not.toMatch(/apply/);
  });

  it("a Closed request's approval is read-only: no Allow once / Deny, and no Done by (dev walk N-F1)", async () => {
    const closed = { ...FIXTURES.step05.tasks[0], status: "resolved" as const, shownGroup: "resolved" as const };
    withDetail({ ...step8, task: closed, questions: [openRow] }, [approval()]);
    pane();
    const card = await screen.findByRole("group", { name: /Provision database and Stripe/ });
    expect(within(card).getByText("Closed — not approved")).toBeInTheDocument();
    expect(within(card).queryByRole("button")).toBeNull();
    expect(screen.getByRole("region", { name: "Task status" })).not.toHaveTextContent("Done by");
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
    const status = await screen.findByRole("region", { name: "Task status" });
    const header = screen.getByRole("banner");
    expect(within(header).getByText("Done")).toBeInTheDocument();
    // Done: no steer box until Follow up (UX audit 2, P0).
    expect(screen.queryByRole("textbox", { name: "Steer this task" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Follow up" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Steer this task" }), "Make it one line{Enter}");
    await waitFor(() => expect(within(header).getByText("Working")).toBeInTheDocument());
    // The lifecycle buttons sit in the header row (W11-2), not on a line of their own under it.
    expect(within(header).getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(within(status).queryByRole("button")).toBeNull();
  });
});

describe("action slots (UX audit 2, P0)", () => {
  const slots = () => [...document.querySelectorAll("[data-action-slot]")].map((el) => `${el.getAttribute("data-action-slot")}:${el.textContent}`);

  it("Stop, Close and Reopen each keep their own slot; the Stop confirm takes their place", async () => {
    const first = pane();
    await screen.findByRole("button", { name: "Stop" });
    expect(slots()).toEqual(["stop:Stop", "retry:", "close:", "reopen:"]);
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(within(screen.getByRole("banner")).getByRole("group", { name: "Stop this task?" })).toBeInTheDocument();
    expect(slots()).toEqual([]);
    first.unmount();
    withDetail({ ...step8, task: { ...step8.task, status: "idle", shownGroup: "idle", watching: false }, questions: [] }, []);
    const second = pane();
    await screen.findByRole("button", { name: "Close" });
    expect(slots()).toEqual(["stop:", "retry:", "close:Close", "reopen:"]);
    second.unmount();
    withDetail({ ...step8, task: { ...step8.task, status: "resolved", shownGroup: "resolved" }, questions: [] }, []);
    pane();
    await screen.findByRole("button", { name: "Reopen" });
    expect(slots()).toEqual(["stop:", "retry:", "close:", "reopen:Reopen"]);
  });

  it("a Closed request offers no steering and no Follow up", async () => {
    withDetail({ ...step8, task: { ...step8.task, status: "resolved", shownGroup: "resolved" }, questions: [] }, []);
    pane();
    await screen.findByRole("button", { name: "Reopen" });
    expect(screen.queryByRole("textbox", { name: "Steer this task" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Follow up" })).toBeNull();
  });
});

describe("one status word, the specialist as the title (audit 02 P2)", () => {
  it("one header: the task's title, then the specialist and Done once, never \"Done by X · Done\" (UX slice 4)", async () => {
    const base = { ...step8.task, status: "idle" as const, shownGroup: "idle" as const, watching: false, specialist: "finance" };
    withDetail({ ...step8, task: { ...base, activity: { n: 1, status: "ended" as const, outcome: "completed" } }, questions: [] }, []);
    pane();
    expect(await screen.findByRole("heading", { level: 3, name: step8.task.title })).toBeInTheDocument();
    expect(within(screen.getByRole("banner")).getByText(/Finance/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Close pane" })).toHaveLength(1);
    const status = await screen.findByRole("region", { name: "Task status" });
    expect(screen.getByRole("complementary", { name: "Task" })).not.toHaveTextContent("Done by");
    // The word once, in the header row beside the specialist (UX slice 3), not again in the body.
    expect(within(screen.getByRole("banner")).getByText("Done")).toBeInTheDocument();
    expect(status).not.toHaveTextContent("Done");
  });
});

describe("task loop (UX slice 3)", () => {
  it("a failed task offers Retry in its own slot, which runs it again", async () => {
    withDetail({ ...step8, task: { ...step8.task, status: "failed", shownGroup: "waiting_on_you", ownerUserId: SAM }, questions: [] }, []);
    pane();
    const retry = await screen.findByRole("button", { name: "Retry" });
    expect(retry.closest("[data-action-slot]")).toHaveAttribute("data-action-slot", "retry");
    await userEvent.click(retry);
    await waitFor(() => expect(client.act).toHaveBeenCalledWith("t-signup", "retry", step8.task.version));
    // The fake answers Closed: the button that took Retry's row is inert for 600 ms, then live.
    const reopen = await screen.findByRole("button", { name: "Reopen" });
    expect(reopen).toBeDisabled();
    await waitFor(() => expect(reopen).toBeEnabled(), { timeout: 1500 });
  });

  it("while it runs, one live line names the specialist and the seconds", async () => {
    withDetail({ ...step8, task: { ...step8.task, updatedAt: new Date(Date.now() - 14_000).toISOString() } });
    pane();
    const status = await screen.findByRole("region", { name: "Task status" });
    expect(status.querySelector("[data-live]")).toHaveTextContent(/^Engineering · working · 1[45]s$/);
  });
});
