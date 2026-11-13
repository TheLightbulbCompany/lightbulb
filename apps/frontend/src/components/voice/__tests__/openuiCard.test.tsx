import { render, screen, waitFor } from "@testing-library/react";
import { beforeAll, expect, it, vi } from "vitest";
import { MarkdownView } from "@/components/voice/MarkdownView";
import { CardContext, type CardContextValue } from "@/components/voice/openui/CardContext";
import { cardMarkdown } from "@/components/voice/cardMarkdown";

// The card is a lazy chunk; the first mount in a file otherwise pays the import inside findByRole's 1 s budget.
beforeAll(() => import("@/components/voice/openui/OpenUICard"));

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
import { capture } from "@/lib/analytics";

const ctx = (over: Partial<CardContextValue> = {}): CardContextValue => ({
  messageId: "card", roomId: "room", authorId: "agent-1", threadRootMessageId: null, attachments: [], answered: null, pending: false, error: null, retry: null, ...over,
});
const fence = (body: string) => "```openui-lang\n" + body + "\n```";

it("a fence the parser cannot turn into a root stays readable as code", async () => {
  render(<CardContext.Provider value={ctx()}><MarkdownView components={cardMarkdown}>{fence("Which plan? Studio or Starter")}</MarkdownView></CardContext.Provider>);
  expect(await screen.findByText("Which plan? Studio or Starter")).toBeInTheDocument();
});

it("renders a Text card from an agent's fence, not a code block", async () => {
  render(<CardContext.Provider value={ctx()}><MarkdownView components={cardMarkdown}>{fence('root = Stack([t])\nt = Text("Hello there")')}</MarkdownView></CardContext.Provider>);
  const text = await screen.findByText("Hello there");
  expect(text.closest("pre")).toBeNull();
  expect(text.closest(".openui-card")).not.toBeNull();
});

it("a human's fence stays code", () => {
  // No CardContext: the message is not an agent's. The fence must render as code, never live UI.
  render(<MarkdownView components={cardMarkdown}>{fence('root = Stack([t])\nt = Text("Hello there")')}</MarkdownView>);
  expect(screen.getByText(/root = Stack/).closest("pre")).not.toBeNull();
});

it("a bogus line still renders the rest, and reports", async () => {
  render(<CardContext.Provider value={ctx()}><MarkdownView components={cardMarkdown}>{fence('root = Stack([t, n])\nt = Text("Kept")\nn = Nope("x")')}</MarkdownView></CardContext.Provider>);
  expect(await screen.findByText("Kept")).toBeInTheDocument();
  expect(capture).toHaveBeenCalledWith("openui.parse_error", expect.objectContaining({ codes: expect.arrayContaining(["unknown-component"]) }));
});

it("a fence past 16 KB renders as code", async () => {
  const big = 'root = Stack([t])\nt = Text("' + "x".repeat(17 * 1024) + '")';
  render(<CardContext.Provider value={ctx()}><MarkdownView components={cardMarkdown}>{fence(big)}</MarkdownView></CardContext.Provider>);
  // The lazy card's fallback is a bare <code>; the <pre> appears once the card has loaded.
  await waitFor(() => expect(document.querySelector("pre")).not.toBeNull());
  expect(document.querySelector(".openui-card")).toBeNull();
});

it("other fences are untouched", () => {
  render(<CardContext.Provider value={ctx()}><MarkdownView components={cardMarkdown}>{"```js\nconsole.log(1)\n```"}</MarkdownView></CardContext.Provider>);
  expect(screen.getByText("console.log(1)").closest("pre")).not.toBeNull();
});
