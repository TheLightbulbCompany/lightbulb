import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MarkdownView } from "@/components/voice/MarkdownView";

describe("MarkdownView", () => {
  it("renders gfm markdown (heading + list)", () => {
    render(<MarkdownView>{"## Section\n\n- first item\n- second item"}</MarkdownView>);
    expect(screen.getByText("Section")).toBeInTheDocument();
    expect(screen.getByText("first item")).toBeInTheDocument();
    expect(screen.getByText("second item")).toBeInTheDocument();
  });

  it("neutralizes a javascript: link (no live link rendered)", () => {
    render(<MarkdownView>{"[click me](javascript:alert(1))"}</MarkdownView>);
    expect(screen.getByText("click me")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("renders a safe https link as a link", () => {
    render(<MarkdownView>{"[docs](https://example.com)"}</MarkdownView>);
    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("href", "https://example.com");
  });

  it("preserves single newlines as line breaks (soft breaks)", () => {
    const { container } = render(<MarkdownView>{"line one\nline two"}</MarkdownView>);
    expect(container.querySelector("br")).not.toBeNull();
  });

  it("wraps in an overflow-safe container so long unbreakable tokens can't overflow", () => {
    // Agent output routinely contains long unbreakable tokens (URLs, error
    // strings). Without min-w-0 + overflow-wrap:anywhere they bleed off the
    // right edge into adjacent columns (observed in the Teams task view).
    const { container } = render(
      <MarkdownView>{"getaddrinfoENOTFOUND https://company.thelightbulb.company/very/long/path"}</MarkdownView>,
    );
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).toContain("min-w-0");
    expect(root.className).toContain("[overflow-wrap:anywhere]");
  });

  it("lets long inline code wrap instead of overflowing", () => {
    const { container } = render(<MarkdownView>{"`getaddrinfoENOTFOUND`"}</MarkdownView>);
    const code = container.querySelector("code");
    expect(code?.className).toContain("[overflow-wrap:anywhere]");
  });
});
