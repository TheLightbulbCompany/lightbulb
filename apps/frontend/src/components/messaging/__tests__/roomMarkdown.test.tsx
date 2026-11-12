import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownView } from "@/components/voice/MarkdownView";
import { roomMarkdown } from "../roomMarkdown";

// The bundled paperclip skill writes internal links as native Paperclip paths
// (e.g. `/ISOA/issues/ISOA-317`), which 404 in a chat room — see roomMarkdown.tsx.
describe("roomMarkdown", () => {
  it("renders an agent-written Paperclip issue link as an in-shell anchor", () => {
    render(
      <MarkdownView skipHtml components={roomMarkdown}>
        {"See [ISOA-317](/ISOA/issues/ISOA-317)"}
      </MarkdownView>,
    );
    const anchor = screen.getByRole("link", { name: "ISOA-317" });
    expect(anchor).toHaveAttribute("href", "/chat?panel=activity");
    expect(anchor).not.toHaveAttribute("target");
  });

  it("leaves an ordinary external link untouched", () => {
    render(
      <MarkdownView skipHtml components={roomMarkdown}>
        {"[docs](https://example.com)"}
      </MarkdownView>,
    );
    const anchor = screen.getByRole("link", { name: "docs" });
    expect(anchor).toHaveAttribute("href", "https://example.com");
    expect(anchor).toHaveAttribute("target", "_blank");
    expect(anchor).toHaveAttribute("rel", "noopener noreferrer");
  });

  // A room message has no owning issue to fetch attachment bytes through
  // (unlike Teams issue views, which use the issue-scoped AttachmentLink) —
  // rendering the upstream attachment href as a normal link 404'd against
  // this app's own host, so it renders as plain text instead.
  it("renders an agent-pasted attachment link as inert plain text, not a broken link", () => {
    render(
      <MarkdownView skipHtml components={roomMarkdown}>
        {"See [the report](/api/attachments/att-1/content)"}
      </MarkdownView>,
    );
    expect(screen.getByText("the report")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "the report" })).not.toBeInTheDocument();
  });
});
