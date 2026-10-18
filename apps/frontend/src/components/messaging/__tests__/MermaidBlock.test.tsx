import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown } from "../roomMarkdown";

vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async (_id: string, src: string) => {
      if (src.includes("garbage")) throw new Error("Parse error");
      return { svg: `<svg data-testid="diagram"><text>${src.split("\n")[0]}</text></svg>` };
    }),
  },
}));

it("draws a mermaid fence as an inline diagram", async () => {
  render(<MarkdownView components={roomMarkdown}>{"```mermaid\ngraph TD; A-->B\n```"}</MarkdownView>);
  expect(await screen.findByRole("img", { name: /diagram/i })).toBeInTheDocument();
  expect(document.querySelector("pre")).toBeNull();
});

it("a bad diagram falls back to code", async () => {
  render(<MarkdownView components={roomMarkdown}>{"```mermaid\ngarbage ;;; \n```"}</MarkdownView>);
  expect(await screen.findByText(/couldn't draw this diagram/i)).toBeInTheDocument();
  expect(screen.getByText(/garbage/).closest("pre")).not.toBeNull();
});

it("other fences are untouched", () => {
  render(<MarkdownView components={roomMarkdown}>{"```js\nconsole.log(1)\n```"}</MarkdownView>);
  expect(screen.getByText("console.log(1)").closest("pre")).not.toBeNull();
});
