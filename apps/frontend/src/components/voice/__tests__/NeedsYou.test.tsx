import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

const q = (over: object) => ({
  id: "q-1", taskId: "t-1", kind: "question", subkind: "didnt_know", title: "Do you do bridal parties?", text: "",
  preview: { kind: "didnt_know", roomId: "r1", caller: "+14155550133", question: "Do you do bridal parties?" },
  state: "open", answerers: [], canAnswer: true, waitingLabel: "Waiting on you", answer: null, answeredBy: null,
  answeredAt: null, createdAt: "2026-11-07T15:00:00Z", version: 1,
  options: [{ id: "call_myself", label: "I'll call them", style: "secondary" }, { id: "done", label: "Done", style: "secondary" }],
  ...over,
});
const qs = vi.hoisted(() => ({ data: [] as object[] }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: vi.fn() }) }));
vi.mock("../data", () => ({
  nameOf: () => "Nova", callNeedsYou: () => false, who: () => "",
  useAgentConfig: () => ({ data: { name: "Nova", knowledge: [], toAdd: [] }, save: vi.fn() }),
  useQuestions: () => ({ data: qs.data, mutate: vi.fn() }),
  useTimeline: () => ({ data: [] }),
}));

import { NeedsYou } from "../NeedsYou";

it("four card kinds render their labels", () => {
  qs.data = [q({ id: "a" }), q({ id: "b", subkind: "handoff" }), q({ id: "c", subkind: "line_problem" }),
    q({ id: "d", kind: "approval", subkind: null })];
  render(<NeedsYou />);
  for (const l of ["Nova didn't know", "Caller wanted a person", "Line problem", "Needs your OK"]) expect(screen.getByText(l)).toBeInTheDocument();
});

it("line problem links to reconnect", () => {
  qs.data = [q({ subkind: "line_problem", title: "Google Calendar stopped working", text: "Reconnect Google Calendar." })];
  render(<NeedsYou />);
  expect(screen.getByRole("link", { name: "Reconnect Google" })).toHaveAttribute("href", "/voice/settings#connections");
});

it("didnt_know offers tel when caller known", () => {
  qs.data = [q({})];
  const { unmount } = render(<NeedsYou />);
  expect(screen.getByRole("link", { name: "I'll call them" })).toHaveAttribute("href", "tel:+14155550133");
  unmount();
  qs.data = [q({ preview: null })];
  render(<NeedsYou />);
  expect(screen.queryByRole("link", { name: "I'll call them" })).toBeNull();
});
