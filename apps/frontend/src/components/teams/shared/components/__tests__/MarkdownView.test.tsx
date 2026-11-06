import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const download = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ download, read: () => ({ data: undefined }) }),
}));

import { MarkdownView } from "../MarkdownView";
import { AttachmentIssueContext, attachmentPathFromHref } from "@/components/teams/shared/lib/attachments";
import { TaskMarkdown } from "@/components/teams/shared/components/TaskMarkdown";
import { roomUrlTransform } from "@/components/messaging/markdown";

describe("attachmentPathFromHref", () => {
  it("maps upstream contentPath hrefs to the issue-scoped BFF path", () => {
    expect(attachmentPathFromHref("/api/attachments/att-1/content", "iss1")).toBe(
      "/issues/iss1/attachments/att-1/content",
    );
    expect(attachmentPathFromHref("/api/attachments/att-1/content?download=1", "iss1")).toBe(
      "/issues/iss1/attachments/att-1/content",
    );
  });

  it("has no path without an owning issue", () => {
    expect(attachmentPathFromHref("/api/attachments/att-1/content", null)).toBeNull();
  });

  it("rejects everything else", () => {
    expect(attachmentPathFromHref(undefined, "iss1")).toBeNull();
    expect(attachmentPathFromHref("https://example.com/api/attachments/x/content", "iss1")).toBeNull();
    expect(attachmentPathFromHref("/api/attachments/att-1/other", "iss1")).toBeNull();
    expect(attachmentPathFromHref("/api/attachments/a%2F..%2Fb/content", "iss1")).toBeNull();
    expect(attachmentPathFromHref("/ACME/issues/ISO-1", "iss1")).toBeNull();
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

  const onIssue = (ui: React.ReactNode) => (
    <AttachmentIssueContext.Provider value="iss-7">{ui}</AttachmentIssueContext.Provider>
  );

  it("downloads an agent-pasted attachment link (issue comment) through the issue-scoped route", async () => {
    render(onIssue(<MarkdownView>{"- Artifact: [ICP rewrite](/api/attachments/att-9/content)"}</MarkdownView>));
    fireEvent.click(screen.getByRole("button", { name: "ICP rewrite" }));
    await waitFor(() => expect(download).toHaveBeenCalledWith("/issues/iss-7/attachments/att-9/content"));
  });

  it("resolves a task-chat attachment link through the issue-scoped route", async () => {
    render(onIssue(<TaskMarkdown body={"Done: [report.pdf](/api/attachments/att-3/content?download=1)"} />));
    fireEvent.click(screen.getByRole("button", { name: "report.pdf" }));
    await waitFor(() => expect(download).toHaveBeenCalledWith("/issues/iss-7/attachments/att-3/content"));
  });

  it("renders an attachment link as plain filename text with no issue context, never fetching", () => {
    render(<MarkdownView>{"See [report.pdf](/api/attachments/att-9/content)"}</MarkdownView>);
    expect(screen.getByText("report.pdf").tagName).toBe("SPAN");
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
    expect(download).not.toHaveBeenCalled();
  });

  it("renders an attachment image from the issue-scoped bytes", async () => {
    const { container } = render(onIssue(<MarkdownView>{"![shot.png](/api/attachments/att-4/content)"}</MarkdownView>));
    await waitFor(() => expect(container.querySelector("img")?.getAttribute("src")).toBe("blob:mock"));
    expect(download).toHaveBeenCalledWith("/issues/iss-7/attachments/att-4/content");
  });

  it("renders an attachment image as its filename with no issue context, never a broken image", () => {
    const { container } = render(<MarkdownView>{"![shot.png](/api/attachments/att-4/content)"}</MarkdownView>);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("shot.png")).toBeInTheDocument();
    expect(download).not.toHaveBeenCalled();
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

describe("MarkdownView unfilled links (W15-6)", () => {
  it("shows a link whose URL is a placeholder as its words, no brackets", () => {
    const { container } = render(<MarkdownView>{"[RESERVE A SPOT →]([REGISTRATION URL]) and [Book](REGISTRATION_URL) or [Go]({{link}})"}</MarkdownView>);
    expect(container.textContent).toBe("RESERVE A SPOT → and Book or Go");
    expect(container.querySelector("a")).toBeNull();
  });

  it("W17-3: the walk-17 email button (bold label, bracketed placeholder URL) renders as a button-styled label", () => {
    const { container } = render(<MarkdownView>{"Tickets are $65 per person.\n\n[**BOOK YOUR SPOT**]([BOOKING LINK])"}</MarkdownView>);
    expect(container.textContent).not.toMatch(/[[\]*]/);
    expect(container.querySelector(".md-cta")?.textContent).toBe("BOOK YOUR SPOT");
    expect(container.querySelector("a")).toBeNull();
  });

  it("keeps real links and code untouched", () => {
    const { container } = render(<MarkdownView>{"[Site](https://example.com/a) `[x](y z)`"}</MarkdownView>);
    expect(container.querySelector("a")?.getAttribute("href")).toBe("https://example.com/a");
    expect(container.querySelector("code")?.textContent).toBe("[x](y z)");
  });
});
