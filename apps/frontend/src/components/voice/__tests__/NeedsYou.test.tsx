import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ post: vi.fn(async () => ({})) }));
const save = vi.hoisted(() => vi.fn(async () => {}));
const q = (over: object) => ({
  id: "q-1", taskId: "t-1", kind: "question", subkind: "didnt_know", title: "Answer Priya: Do you do bridal parties?",
  text: "Caller: +14155550133", preview: null, state: "open", askedBy: null, answerers: [], canAnswer: true,
  waitingLabel: "Waiting on you", answer: null, answeredBy: null, answeredAt: null, reason: null, cancelledBy: null,
  cancelledAt: null, govRef: null, createdAt: "2026-11-07T15:00:00Z", version: 1,
  options: [{ id: "answer", label: "Answer", style: "primary" }, { id: "call_myself", label: "I'll call them", style: "secondary" },
    { id: "done", label: "Done", style: "secondary" }],
  ...over,
});
const call = (over: object) => ({
  id: "q-1", taskId: "t-1", kind: "question", subkind: "didnt_know", title: "Do you do bridal parties?", text: "",
  preview: { kind: "didnt_know", roomId: "r1", caller: "+14155550133", question: "Do you do bridal parties?" },
  state: "open", answerers: [], canAnswer: true, waitingLabel: "Waiting on you", answer: null, answeredBy: null,
  answeredAt: null, createdAt: "2026-11-07T15:00:00Z", version: 1,
  options: [{ id: "call_myself", label: "I'll call them", style: "secondary" }, { id: "done", label: "Done", style: "secondary" }],
  ...over,
});
const qs = vi.hoisted(() => ({ data: [] as object[] }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
vi.mock("../data", () => ({
  nameOf: () => "Nova",
  useAgentConfig: () => ({ data: { name: "Nova", knowledge: [], toAdd: [] }, save }),
  useQuestions: () => ({ data: qs.data, mutate: vi.fn() }),
  useTimeline: () => ({ data: [] }),
}));

import { NeedsYou } from "../NeedsYou";

it("a call's didn't-know Question answers with text, and the backend (not this admin-only save) writes Knowledge", async () => {
  qs.data = [q({})];
  render(<NeedsYou />);
  expect(screen.getByText("Nova didn't know")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "I'll call them" })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Your answer"), { target: { value: "Yes, up to eight." } });
  fireEvent.click(screen.getByRole("button", { name: "Answer" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/tasks/t-1/questions/q-1/answer",
    expect.objectContaining({ optionId: "answer", text: "Yes, up to eight." })));
  expect(save).not.toHaveBeenCalled();
});

it("a call's handoff Question reads as the caller wanting a person", () => {
  qs.data = [q({ id: "q-2", subkind: "handoff", title: "Priya wanted a person",
    options: [{ id: "call_myself", label: "I'll call them", style: "primary" }, { id: "done", label: "Done", style: "secondary" }] })];
  render(<NeedsYou />);
  expect(screen.getByText("Caller wanted a person")).toBeInTheDocument();
  expect(screen.queryByLabelText("Your answer")).toBeNull();
});

it("four card kinds render their labels", () => {
  qs.data = [call({ id: "a" }), call({ id: "b", subkind: "handoff" }), call({ id: "c", subkind: "line_problem" }),
    call({ id: "d", kind: "approval", subkind: null })];
  render(<NeedsYou />);
  for (const l of ["Nova didn't know", "Caller wanted a person", "Line problem", "Needs your OK"]) expect(screen.getByText(l)).toBeInTheDocument();
});

it("line problem links to reconnect", () => {
  qs.data = [call({ subkind: "line_problem", title: "Google Calendar stopped working", text: "Reconnect Google Calendar." })];
  render(<NeedsYou />);
  expect(screen.getByRole("link", { name: "Reconnect Google" })).toHaveAttribute("href", "/voice/settings#connections");
});

it("didnt_know offers tel when caller known", () => {
  qs.data = [call({})];
  const { unmount } = render(<NeedsYou />);
  expect(screen.getByRole("link", { name: "I'll call them" })).toHaveAttribute("href", "tel:+14155550133");
  unmount();
  qs.data = [call({ preview: null })];
  render(<NeedsYou />);
  expect(screen.queryByRole("link", { name: "I'll call them" })).toBeNull();
});
