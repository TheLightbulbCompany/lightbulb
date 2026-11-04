import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ApiError, type LocalSession, type LocalSessionCommand } from "@lightbulb/shared";
import { SessionQuestionCard } from "../SessionQuestion";
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "alice" }) }));
const session: LocalSession = { sessionId: "s", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "test", generation: "g1", status: "waiting-approval", activeTurnId: null,
  capabilities: { sendMode: "none", steer: false, interrupt: false }, capabilitiesVerified: false, controlUnavailableReason: null, latestEventSeq: 0,
  pendingQuestion: { promptId: "q1", inputDigest: "abc", questions: [{ question: "Which approach?", options: [{ label: "Small change" }, { label: "Rebuild" }] }] } };
it("answers a plain session question without terminal input or ordinary-send capability", async () => {
  const submitLocalSessionCommand = vi.fn(async () => ({ state: "applied" }));
  render(<SessionQuestionCard session={session} client={{ submitLocalSessionCommand } as never} own ownerName="Alice" />);
  fireEvent.click(screen.getByRole("button", { name: "Small change" }));
  fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
  await screen.findByText("Answered");
  expect(submitLocalSessionCommand).toHaveBeenCalledWith("s", expect.objectContaining({ kind: "answer", expectedGeneration: "g1", promptId: "q1", inputDigest: "abc", answers: { "Which approach?": "Small change" } }));
  expect(submitLocalSessionCommand).toHaveBeenCalledTimes(1);
});
it("preserves custom text and never submits twice after an acknowledgement is lost", async () => {
  const submitLocalSessionCommand = vi.fn(async () => { throw new TypeError("Failed to fetch"); });
  render(<SessionQuestionCard session={session} client={{ submitLocalSessionCommand } as never} own ownerName="Alice" />);
  fireEvent.change(screen.getByLabelText("Your answer"), { target: { value: "Use the existing interface" } });
  fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
  await screen.findByText("Waiting for Claude to receive your answer…");
  fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
  expect(screen.getByLabelText("Your answer")).toBeDisabled();
  expect(submitLocalSessionCommand).toHaveBeenCalledTimes(1);
});
it("checks the same request after submission until Claude receives it", async () => {
  vi.useFakeTimers();
  const submitLocalSessionCommand = vi.fn<(sessionId: string, request: LocalSessionCommand) => Promise<{ state: string }>>().mockResolvedValue({ state: "pending" });
  const getLocalSessionCommand = vi.fn(async () => ({ state: "applied" }));
  render(<SessionQuestionCard session={session} client={{ submitLocalSessionCommand, getLocalSessionCommand } as never} own ownerName="Alice" />);
  fireEvent.click(screen.getByRole("button", { name: "Rebuild" }));
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send answer" })); });
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByText("Answered")).toBeInTheDocument();
  expect(getLocalSessionCommand).toHaveBeenCalledWith("s", submitLocalSessionCommand.mock.calls[0]?.[1]?.requestId);
  vi.useRealTimers();
});
it("does not let an unauthorized viewer answer", () => {
  const submitLocalSessionCommand = vi.fn();
  render(<SessionQuestionCard session={session} client={{ submitLocalSessionCommand }} own={false} ownerName="Alice" />);
  expect(screen.getByRole("button", { name: "Small change" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Send answer" })).toBeNull();
});

it("handles question text that is also a JavaScript prototype property", async () => {
  const submitLocalSessionCommand = vi.fn(async () => ({ state: "applied" }));
  render(<SessionQuestionCard session={{ ...session, pendingQuestion: { ...session.pendingQuestion!, questions: [{ question: "constructor", options: [{ label: "Continue", description: "Keep the same conversation" }] }] } }} client={{ submitLocalSessionCommand } as never} own ownerName="Alice" />);
  expect(screen.getByText("Keep the same conversation")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /Continue/ }));
  fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
  await screen.findByText("Answered");
  expect(submitLocalSessionCommand).toHaveBeenCalledWith("s", expect.objectContaining({ answers: { constructor: "Continue" } }));
});
it("keeps receipt checks running through repeated same-prompt refreshes", async () => {
  vi.useFakeTimers();
  try {
    const submitLocalSessionCommand = vi.fn(async () => ({ state: "pending" }));
    const getLocalSessionCommand = vi.fn(async () => ({ state: "applied" }));
    const client = { submitLocalSessionCommand, getLocalSessionCommand } as never;
    const view = render(<SessionQuestionCard session={session} client={client} own ownerName="Alice" />);
    fireEvent.click(screen.getByRole("button", { name: "Small change" }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send answer" })); });
    for (let i = 0; i < 4; i++) {
      await act(async () => { await vi.advanceTimersByTimeAsync(600); });
      view.rerender(<SessionQuestionCard session={{ ...session, pendingQuestion: { ...session.pendingQuestion! } }} client={client} own ownerName="Alice" />);
    }
    expect(getLocalSessionCommand).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Answered")).toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});
it("stops retrying when a lost submission is definitively refused", async () => {
  vi.useFakeTimers();
  try {
    const submitLocalSessionCommand = vi.fn().mockRejectedValueOnce(new TypeError("Lost request")).mockRejectedValue(new ApiError(409, { code: "LOCAL_SESSION_STALE" }, "Question closed"));
    const getLocalSessionCommand = vi.fn().mockRejectedValue(new ApiError(404, {}, "Not found"));
    render(<SessionQuestionCard session={session} client={{ submitLocalSessionCommand, getLocalSessionCommand } as never} own ownerName="Alice" />);
    fireEvent.click(screen.getByRole("button", { name: "Small change" }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send answer" })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(submitLocalSessionCommand).toHaveBeenCalledTimes(2);
    expect(submitLocalSessionCommand.mock.calls[1]).toEqual(submitLocalSessionCommand.mock.calls[0]);
    expect(screen.getByRole("alert")).toHaveTextContent("Question closed");
  } finally { vi.useRealTimers(); }
});
it("does not enable questions while sharing is paused", () => {
  render(<SessionQuestionCard session={{ ...session, shared: false }} client={{ submitLocalSessionCommand: vi.fn() }} own ownerName="Alice" />);
  expect(screen.getByRole("button", { name: "Small change" })).toBeDisabled();
});
