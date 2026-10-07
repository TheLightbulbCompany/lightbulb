import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
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
    expect(anchor).toHaveAttribute("href", "/chat?panel=teams-issue-detail&detailId=ISOA-317");
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
});
