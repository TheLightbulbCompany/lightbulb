import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const download = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ download }),
}));

import {
  MarkdownView,
  attachmentPathFromHref,
  shellUrlFromPaperclipPath,
} from "../MarkdownView";

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

describe("shellUrlFromPaperclipPath", () => {
  it("maps company-prefixed issue paths to the in-shell detail URL", () => {
    expect(shellUrlFromPaperclipPath("/ISOA/issues/ISOA-171")).toBe(
      "/chat?panel=teams-issue-detail&detailId=ISOA-171",
    );
    expect(shellUrlFromPaperclipPath("/PAP/issues/PAP-224#comment-abc")).toBe(
      "/chat?panel=teams-issue-detail&detailId=PAP-224#comment-abc",
    );
    expect(shellUrlFromPaperclipPath("/PAP/issues/PAP-224#document-plan")).toBe(
      "/chat?panel=teams-issue-detail&detailId=PAP-224#document-plan",
    );
  });

  it("maps run paths to the run detail panel", () => {
    expect(shellUrlFromPaperclipPath("/PAP/agents/cto/runs/run-9")).toBe(
      "/chat?panel=teams-run-detail&detailId=run-9",
    );
  });

  it("lands shapes with no detail panel on the list panel that owns them", () => {
    expect(shellUrlFromPaperclipPath("/PAP/approvals/ca6ba09d-b558")).toBe(
      "/chat?panel=teams-inbox",
    );
    expect(shellUrlFromPaperclipPath("/PAP/agents/cto")).toBe("/chat?panel=teams-agents");
    expect(shellUrlFromPaperclipPath("/PAP/projects/growth")).toBe(
      "/chat?panel=teams-projects",
    );
  });

  it("leaves everything else alone", () => {
    expect(shellUrlFromPaperclipPath(undefined)).toBeNull();
    expect(shellUrlFromPaperclipPath("")).toBeNull();
    // External URLs, including ones whose path looks company-prefixed.
    expect(shellUrlFromPaperclipPath("https://example.com/docs")).toBeNull();
    expect(shellUrlFromPaperclipPath("https://paperclip.example/PAP/issues/PAP-1")).toBeNull();
    // Attachment downloads stay with the authenticated AttachmentLink path.
    expect(shellUrlFromPaperclipPath("/api/attachments/att-1/content")).toBeNull();
    // Unrelated absolute paths — lowercase prefixes must not match.
    expect(shellUrlFromPaperclipPath("/chat?panel=teams-inbox")).toBeNull();
    expect(shellUrlFromPaperclipPath("/settings")).toBeNull();
    expect(shellUrlFromPaperclipPath("/PAP/sprints/1")).toBeNull();
    // Unsafe schemes never reach here (they can't start with `/`), and must not
    // be smuggled past safeHref by this helper either.
    expect(shellUrlFromPaperclipPath("javascript:alert(1)")).toBeNull();
    expect(shellUrlFromPaperclipPath("data:text/html,<script>")).toBeNull();
  });
});

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
