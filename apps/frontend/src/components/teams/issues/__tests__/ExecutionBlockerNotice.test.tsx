// Adapted from upstream Paperclip's ExecutionBlockerNotice.test.tsx
// (paperclip/ui/src/components/ExecutionBlockerNotice.test.tsx @ v2026.1012.0-isol8.4).

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const { readSpy, postSpy, mutateSpy } = vi.hoisted(() => ({
  readSpy: vi.fn(),
  postSpy: vi.fn(),
  mutateSpy: vi.fn(),
}));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read: readSpy, post: postSpy, mutate: mutateSpy }),
}));

import { ExecutionBlockerNotice } from "@/components/teams/issues/ExecutionBlockerNotice";
import { teamsDetailUrl } from "@/lib/shellNav";

const LEGACY_BLOCKER = {
  recoveryActionId: "recovery",
  runId: "failed-run",
  agentId: "agent",
  cause: "legacy_execution_requires_reconciliation",
  nextAction: "Automatic recovery stopped. Recorded work is preserved; actions with unverified outcomes will not be repeated.",
};

beforeEach(() => {
  readSpy.mockReset().mockReturnValue({ data: { runs: [{ runId: "failed-run", agentId: "agent", status: "failed" }] } });
  postSpy.mockReset();
  mutateSpy.mockReset();
});

describe("ExecutionBlockerNotice (ported)", () => {
  it('says "Model unavailable." for native_provider_model_rejected', () => {
    readSpy.mockReturnValue({ data: { runs: [{ runId: "failed-run", agentId: "agent", status: "failed", errorCode: "native_provider_model_rejected" }] } });
    render(<ExecutionBlockerNotice issueId="task" blocker={{ ...LEGACY_BLOCKER, runError: "Model x is not available." }} />);
    const text = screen.getByRole("status", { name: "Task recovery" }).textContent;
    expect(text).toContain("Model unavailable. Model x is not available.");
    expect(text).toContain("Choose a supported model or clear the task's model override, then retry.");
    expect(text).not.toContain("Recovery needed.");
  });

  it('says "Recovery needed." otherwise, then runError then nextAction', () => {
    render(<ExecutionBlockerNotice issueId="task" blocker={{ ...LEGACY_BLOCKER, runError: "Provider cancelled execution" }} />);
    const notice = screen.getByRole("status", { name: "Task recovery" });
    const paragraphs = Array.from(notice.querySelectorAll("p")).map(p => p.textContent);
    expect(paragraphs[0]).toBe("Recovery needed. Provider cancelled execution");
    expect(paragraphs[1]).toBe(LEGACY_BLOCKER.nextAction);
    expect(notice.textContent).not.toContain("Choose a supported model");
    expect(notice.classList.contains("border")).toBe(true);
    expect(notice.classList.contains("bg-muted")).toBe(true);
    expect(readSpy).toHaveBeenCalledWith("/issues/task/runs");
  });

  it("shows the saved-message line when savedMessageCount > 0", () => {
    const { rerender } = render(<ExecutionBlockerNotice issueId="task" blocker={{ ...LEGACY_BLOCKER, savedMessageCount: 2 }} />);
    expect(screen.getByRole("status").textContent).toContain("2 saved messages are waiting for recovery.");
    rerender(<ExecutionBlockerNotice issueId="task" blocker={{ ...LEGACY_BLOCKER, savedMessageCount: 1 }} />);
    expect(screen.getByRole("status").textContent).toContain("1 saved message is waiting for recovery.");
    rerender(<ExecutionBlockerNotice issueId="task" blocker={{ ...LEGACY_BLOCKER, savedMessageCount: 0 }} />);
    expect(screen.getByRole("status").textContent).not.toContain("saved");
  });

  it("Inspect run links to the run detail", () => {
    render(<ExecutionBlockerNotice issueId="task" blocker={LEGACY_BLOCKER} />);
    const link = screen.getByRole("link", { name: "Inspect run" });
    expect(link.getAttribute("href")).toBe(teamsDetailUrl("run", "failed-run"));
  });

  it("Retry shown when canRetry, hidden when requiresInspection && !canRetry", () => {
    const quarantined = { ...LEGACY_BLOCKER, cause: "native_session_cleanup_quarantined" };
    const { rerender } = render(<ExecutionBlockerNotice issueId="task" blocker={{ ...quarantined, canRetry: true }} />);
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    rerender(<ExecutionBlockerNotice issueId="task" blocker={{ ...quarantined, canRetry: false }} />);
    expect(screen.queryByRole("button")).toBeNull();
    rerender(<ExecutionBlockerNotice issueId="task" blocker={{ ...LEGACY_BLOCKER, cause: "native_continuation_requires_reconciliation" }} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("shows Continue when canContinue and hides the button when nothing is retryable", () => {
    readSpy.mockReturnValue({ data: { runs: [{ runId: "failed-run", agentId: "agent", status: "running" }] } });
    const { rerender } = render(<ExecutionBlockerNotice issueId="task" blocker={LEGACY_BLOCKER} />);
    expect(screen.queryByRole("button")).toBeNull();
    rerender(<ExecutionBlockerNotice issueId="task" blocker={{ ...LEGACY_BLOCKER, canContinue: true }} />);
    expect(screen.getByRole("button", { name: "Continue" })).toBeInTheDocument();
  });

  it("retries the exact failed run and refreshes the task", async () => {
    postSpy.mockResolvedValue({ id: "new-run", status: "queued" });
    const onRetried = vi.fn();
    render(<ExecutionBlockerNotice issueId="task" blocker={LEGACY_BLOCKER} onRetried={onRetried} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(onRetried).toHaveBeenCalledOnce());
    expect(postSpy).toHaveBeenCalledWith("/agents/agent/wakeup", {
      source: "on_demand",
      triggerDetail: "manual",
      reason: "retry_failed_run",
      failedRunId: "failed-run",
    });
    for (const key of ["/teams/issues/task", "/teams/issues/task/runs", "/teams/issues/task/live-runs", "/teams/issues/task/active-run"]) {
      expect(mutateSpy).toHaveBeenCalledWith(key);
    }
  });

  it("treats an already-stopped successor as a failed retry", async () => {
    postSpy.mockResolvedValue({ id: "new-run", status: "failed" });
    render(<ExecutionBlockerNotice issueId="task" blocker={LEGACY_BLOCKER} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The previous retry has already stopped.");
  });

  it("shows a failed Retry in the same container and allows another attempt", async () => {
    postSpy.mockRejectedValue(new Error("Environment cleanup is still running."));
    const onRetried = vi.fn();
    render(<ExecutionBlockerNotice issueId="task" blocker={LEGACY_BLOCKER} onRetried={onRetried} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Environment cleanup is still running.");
    expect(screen.getByRole("button", { name: "Retry" })).not.toBeDisabled();
    expect(onRetried).not.toHaveBeenCalled();
  });

  it("treats a skipped wakeup as a failed retry", async () => {
    postSpy.mockResolvedValue({ status: "skipped", message: "Agent is paused." });
    render(<ExecutionBlockerNotice issueId="task" blocker={LEGACY_BLOCKER} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Agent is paused.");
  });
});
