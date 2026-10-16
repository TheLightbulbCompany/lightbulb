import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { CardContext, type CardContextValue } from "../openui/CardContext";
import { roomMarkdown } from "../roomMarkdown";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
import { capture } from "@/lib/analytics";

const ctx = (over: Partial<CardContextValue> = {}): CardContextValue => ({
  messageId: "card", roomId: "room", authorId: "agent-1", threadRootMessageId: null, attachments: [], answered: null, ...over,
});
const fence = (body: string) => "```openui-lang\n" + body + "\n```";

it("renders a Text card from an agent's fence, not a code block", () => {
  render(<CardContext.Provider value={ctx()}><MarkdownView components={roomMarkdown}>{fence('root = Stack([t])\nt = Text("Hello there")')}</MarkdownView></CardContext.Provider>);
  expect(screen.getByText("Hello there").closest("pre")).toBeNull();
  expect(screen.getByText("Hello there").closest(".openui-card")).not.toBeNull();
});

it("a human's fence stays code", () => {
  // No CardContext: the message is not an agent's. The fence must render as code, never live UI.
  render(<MarkdownView components={roomMarkdown}>{fence('root = Stack([t])\nt = Text("Hello there")')}</MarkdownView>);
  expect(screen.getByText(/root = Stack/).closest("pre")).not.toBeNull();
});

it("a bogus line still renders the rest, and reports", () => {
  render(<CardContext.Provider value={ctx()}><MarkdownView components={roomMarkdown}>{fence('root = Stack([t, n])\nt = Text("Kept")\nn = Nope("x")')}</MarkdownView></CardContext.Provider>);
  expect(screen.getByText("Kept")).toBeInTheDocument();
  expect(capture).toHaveBeenCalledWith("openui.parse_error", expect.objectContaining({ codes: expect.arrayContaining(["unknown-component"]) }));
});

it("a fence past 16 KB renders as code", () => {
  const big = 'root = Stack([t])\nt = Text("' + "x".repeat(17 * 1024) + '")';
  render(<CardContext.Provider value={ctx()}><MarkdownView components={roomMarkdown}>{fence(big)}</MarkdownView></CardContext.Provider>);
  expect(document.querySelector(".openui-card")).toBeNull();
  expect(document.querySelector("pre")).not.toBeNull();
});

it("other fences are untouched", () => {
  render(<CardContext.Provider value={ctx()}><MarkdownView components={roomMarkdown}>{"```js\nconsole.log(1)\n```"}</MarkdownView></CardContext.Provider>);
  expect(screen.getByText("console.log(1)").closest("pre")).not.toBeNull();
});
