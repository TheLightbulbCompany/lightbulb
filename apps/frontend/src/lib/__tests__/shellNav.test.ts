// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { navigateShell, pushSearchParams, setCurrentEntryNamer, shellUrlFromPaperclipPath } from "../shellNav";

describe("setCurrentEntryNamer", () => {
  it("names a command-less entry before a push leaves it, and only that", () => {
    window.history.replaceState(null, "", "/chat");
    const namer = vi.fn(() => "panel=chat&room=r1");
    setCurrentEntryNamer(namer);
    const replace = vi.spyOn(window.history, "replaceState");
    pushSearchParams(new URLSearchParams("panel=teams-issues"));
    expect(replace).toHaveBeenCalledExactlyOnceWith(null, "", "/chat?panel=chat&room=r1");
    navigateShell("/chat?panel=mail");
    expect(replace).toHaveBeenCalledTimes(1);
    window.history.replaceState(null, "", "/chat?agent=a1");
    replace.mockClear();
    pushSearchParams(new URLSearchParams("panel=teams-issues"));
    expect(replace).not.toHaveBeenCalled();
    window.history.replaceState(null, "", "/chat");
    setCurrentEntryNamer(null);
    replace.mockClear();
    navigateShell("/chat?panel=mail");
    expect(replace).not.toHaveBeenCalled();
    replace.mockRestore();
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

  it("maps goal paths to the goal detail panel", () => {
    expect(shellUrlFromPaperclipPath("/PAP/goals/g-142")).toBe(
      "/chat?panel=teams-goal-detail&detailId=g-142",
    );
  });

  it("maps agent paths to the agent inspector, and project paths to project detail", () => {
    expect(shellUrlFromPaperclipPath("/PAP/approvals/ca6ba09d-b558")).toBe(
      "/chat?panel=teams-approval-detail&detailId=ca6ba09d-b558",
    );
    // Carries the raw id through unresolved — the panel/hook resolves a
    // non-uuid (url-key) id against the loaded roster/project list.
    expect(shellUrlFromPaperclipPath("/PAP/agents/cto")).toBe(
      "/chat?panel=agent-inspector&agent=cto",
    );
    expect(shellUrlFromPaperclipPath("/PAP/projects/growth")).toBe(
      "/chat?panel=teams-project-detail&detailId=growth",
    );
  });

  it("tolerates a dropped company prefix for issues and approvals only", () => {
    expect(shellUrlFromPaperclipPath("/issues/ISOA-171")).toBe(
      "/chat?panel=teams-issue-detail&detailId=ISOA-171",
    );
    expect(shellUrlFromPaperclipPath("/approvals/ca6ba09d-b558")).toBe(
      "/chat?panel=teams-approval-detail&detailId=ca6ba09d-b558",
    );
    // Goals/agents/projects keep the prefix mandatory — no company-scoped
    // list to safely fall back to the way issues/approvals do.
    expect(shellUrlFromPaperclipPath("/goals/g-142")).toBeNull();
    expect(shellUrlFromPaperclipPath("/agents/cto")).toBeNull();
    expect(shellUrlFromPaperclipPath("/projects/growth")).toBeNull();
  });

  it("reads the retired /teams/tasks and /teams/issues links agents still write as issue links", () => {
    expect(shellUrlFromPaperclipPath("/teams/tasks/ISOA-12")).toBe("/chat?panel=teams-issue-detail&detailId=ISOA-12");
    expect(shellUrlFromPaperclipPath("/teams/issues/ISOA-12")).toBe("/chat?panel=teams-issue-detail&detailId=ISOA-12");
    expect(shellUrlFromPaperclipPath("/teams/inbox")).toBeNull();
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
