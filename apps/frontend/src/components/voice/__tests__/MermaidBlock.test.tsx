import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MarkdownView } from "@/components/voice/MarkdownView";
import { cardMarkdown } from "@/components/voice/cardMarkdown";
import mermaid from "mermaid";

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
  render(<MarkdownView components={cardMarkdown}>{"```mermaid\ngraph TD; A-->B\n```"}</MarkdownView>);
  expect(await screen.findByRole("img", { name: /diagram/i })).toBeInTheDocument();
  expect(document.querySelector("pre")).toBeNull();
  // Review Focus 5 rests on this one setting: labels are sanitised.
  expect(mermaid.initialize).toHaveBeenCalledWith(expect.objectContaining({ securityLevel: "strict", suppressErrorRendering: true }));
});

it("re-initialises before every diagram, so the theme follows a toggle", async () => {
  vi.mocked(mermaid.initialize).mockClear();
  render(<MarkdownView components={cardMarkdown}>{"```mermaid\ngraph TD; C-->D\n```"}</MarkdownView>);
  await screen.findByRole("img", { name: /diagram/i });
  expect(mermaid.initialize).toHaveBeenCalledTimes(1);
});

it("a bad diagram falls back to code", async () => {
  render(<MarkdownView components={cardMarkdown}>{"```mermaid\ngarbage ;;; \n```"}</MarkdownView>);
  expect(await screen.findByText(/couldn't draw this diagram/i)).toBeInTheDocument();
  expect(screen.getByText(/garbage/).closest("pre")).not.toBeNull();
});

it("measures labels in the page's own font and draws nothing for an empty fence", async () => {
  const { default: mermaid } = await import("mermaid");
  render(<MarkdownView components={cardMarkdown}>{"```mermaid\ngraph TD; A-->B\n```"}</MarkdownView>);
  await screen.findByRole("img", { name: /diagram/i });
  const font = getComputedStyle(document.body).fontFamily || "inherit";
  expect(mermaid.initialize).toHaveBeenCalledWith(expect.objectContaining({ fontFamily: font, themeVariables: expect.objectContaining({ fontFamily: font }) }));
  const { container } = render(<MarkdownView components={cardMarkdown}>{"before\n\n```\n```\n\nafter"}</MarkdownView>);
  expect(container.querySelector("pre")).toBeNull();
  expect(container.textContent).toContain("after");
});

it("other fences are untouched", () => {
  render(<MarkdownView components={cardMarkdown}>{"```js\nconsole.log(1)\n```"}</MarkdownView>);
  expect(screen.getByText("console.log(1)").closest("pre")).not.toBeNull();
});
