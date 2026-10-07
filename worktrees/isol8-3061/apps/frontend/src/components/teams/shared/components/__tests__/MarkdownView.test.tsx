import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const download = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ download }),
}));

import { MarkdownView, attachmentPathFromHref } from "../MarkdownView";
import { roomUrlTransform } from "@/components/messaging/markdown";

describe("attachmentPathFromHref", () => {
  it("maps upstream contentPath hrefs to the BFF download path", () => {
    expect(attachmentPathFromHref("/api/attachments/att-1/content")).toBe(
      "/attachments/att-1/content",
    );
    expect(attachmentPathFromHref("/api/attachments/att-1/content?download=1")).toBe(
      "/attachments/att-1/content",
    );
  });

  it("rejects everything else", () => {
    expect(attachmentPathFromHref(undefined)).toBeNull();
    expect(attachmentPathFromHref("https://example.com/api/attachments/x/content")).toBeNull();
    expect(attachmentPathFromHref("/api/attachments/att-1/other")).toBeNull();
    expect(attachmentPathFromHref("/ACME/issues/ISO-1")).toBeNull();
  });
});

// shellUrlFromPaperclipPath's own unit tests moved to
// src/lib/__tests__/shellNav.test.ts alongside its new home. These cover
// MarkdownView's rendering/click behavior built on top of it.
describe("MarkdownView paperclip links", () => {
  it("renders an agent-written issue link as an in-shell anchor", () => {
    render(<MarkdownView>{"See [ISOA-171](/ISOA/issues/ISOA-171)"}</MarkdownView>);
    const anchor = screen.getByRole("link", { name: "ISOA-171" });
    expect(anchor).toHaveAttribute("href", "/chat?panel=teams-issue-detail&detailId=ISOA-171");
    expect(anchor).not.toHaveAttribute("target");
  });

  it("commits a plain click through pushState when the shell is on /chat", () => {
    window.history.replaceState(null, "", "/chat");
    const push = vi.spyOn(window.history, "pushState");
    render(<MarkdownView>{"See [ISOA-171](/ISOA/issues/ISOA-171)"}</MarkdownView>);
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
    screen.getByRole("link", { name: "ISOA-171" }).dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(push).toHaveBeenCalledWith(
      null,
      "",
      "/chat?panel=teams-issue-detail&detailId=ISOA-171",
    );
    push.mockRestore();
  });

  it("lets the anchor navigate natively off the shell route (landing demo)", () => {
    window.history.replaceState(null, "", "/");
    const push = vi.spyOn(window.history, "pushState");
    render(<MarkdownView>{"See [ISOA-171](/ISOA/issues/ISOA-171)"}</MarkdownView>);
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
    screen.getByRole("link", { name: "ISOA-171" }).dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(push).not.toHaveBeenCalled();
    push.mockRestore();
  });

  it("drops links with an unsafe scheme", () => {
    render(<MarkdownView>{"[boom](javascript:alert(1))"}</MarkdownView>);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("boom")).toBeTruthy();
  });
});

describe("MarkdownView attachment links", () => {
  beforeEach(() => {
    download.mockReset().mockResolvedValue(new Blob(["hi"]));
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
  });

  it("renders an agent-pasted attachment link as an authenticated download", async () => {
    render(
      <MarkdownView>
        {"- Artifact: [ICP rewrite](/api/attachments/att-9/content)"}
      </MarkdownView>,
    );
    const button = screen.getByRole("button", { name: "ICP rewrite" });
    fireEvent.click(button);
    await waitFor(() => expect(download).toHaveBeenCalledWith("/attachments/att-9/content"));
  });

  it("leaves ordinary links as plain anchors", () => {
    render(<MarkdownView>{"[docs](https://example.com/docs)"}</MarkdownView>);
    const anchor = screen.getByRole("link", { name: "docs" });
    expect(anchor).toHaveAttribute("href", "https://example.com/docs");
  });
});

describe("MarkdownView urlTransform", () => {
  it("passes a mention: href through when the caller opts in (roomUrlTransform)", () => {
    render(<MarkdownView urlTransform={roomUrlTransform}>{"[@x](mention:agent:1)"}</MarkdownView>);
    expect(screen.getByRole("link", { name: "@x" })).toHaveAttribute("href", "mention:agent:1");
  });

  it("still blanks a javascript: href with the default transform", () => {
    render(<MarkdownView>{"[boom](javascript:alert(1))"}</MarkdownView>);
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("still blanks a data: href with the default transform", () => {
    render(<MarkdownView>{"[boom](data:text/html,<script>)"}</MarkdownView>);
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("still blanks a javascript: href with the mention passthrough", () => {
    render(<MarkdownView urlTransform={roomUrlTransform}>{"[boom](javascript:alert(1))"}</MarkdownView>);
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("still blanks a data: href with the mention passthrough", () => {
    render(<MarkdownView urlTransform={roomUrlTransform}>{"[boom](data:text/html,<script>)"}</MarkdownView>);
    expect(screen.queryByRole("link")).toBeNull();
  });
});

describe("MarkdownView extension points", () => {
  it("renders raw HTML comments as text by default (react-markdown 10 behaviour)", () => {
    render(<MarkdownView>{"a\n\n<!-- openclaw:wiki:index:start -->\n\nb"}</MarkdownView>);
    expect(screen.getByText(/openclaw:wiki:index:start/)).toBeInTheDocument();
  });

  it("skipHtml drops raw HTML entirely", () => {
    render(<MarkdownView skipHtml>{"a\n\n<!-- openclaw:wiki:index:start -->\n\nb"}</MarkdownView>);
    expect(screen.queryByText(/openclaw:wiki/)).toBeNull();
  });

  it("components override replaces a shared component", () => {
    render(
      <MarkdownView components={{ a: ({ children }) => <span data-testid="custom-a">{children}</span> }}>
        {"[x](entities/x.md)"}
      </MarkdownView>,
    );
    expect(screen.getByTestId("custom-a")).toHaveTextContent("x");
  });
});
