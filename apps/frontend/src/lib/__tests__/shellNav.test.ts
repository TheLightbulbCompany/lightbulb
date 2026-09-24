import { describe, it, expect } from "vitest";
import { shellUrlFromPaperclipPath } from "../shellNav";

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
      "/chat?panel=teams-approval-detail&detailId=ca6ba09d-b558",
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
