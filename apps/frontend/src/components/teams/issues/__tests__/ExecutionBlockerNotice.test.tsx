// Adapted from upstream Paperclip's ExecutionBlockerNotice.test.tsx
// (paperclip/ui/src/components/ExecutionBlockerNotice.test.tsx @ v2026.916.1-isol8.2).

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

const LEGACY_BLOCKER = {
  recoveryActionId: "recovery",
  runId: "failed-run",
  agentId: "agent",
  cause: "legacy_execution_requires_reconciliation",
  nextAction: "Automatic recovery stopped. Recorded work is preserved; actions with unverified outcomes will not be repeated.",
};
// Paperclip marks a run it will accept a Retry for; only then is Retry offered.
const RETRYABLE = { ...LEGACY_BLOCKER, canRetry: true };

beforeEach(() => {
  readSpy.mockReset().mockReturnValue({ data: { runs: [{ runId: "failed-run", agentId: "agent", status: "failed" }] } });
  postSpy.mockReset();
  mutateSpy.mockReset();
});

describe("ExecutionBlockerNotice (ported)", () => {
  it("shows only the requested sentence and Retry, inside a distinct recovery container", () => {
    render(<ExecutionBlockerNotice issueId="task" blocker={RETRYABLE} />);
    const notice = screen.getByRole("status", { name: "Task recovery" });
    expect(notice.textContent).toBe("Automatic recovery of this task stopped.Retry");
    expect(notice.classList.contains("border")).toBe(true);
    expect(notice.classList.contains("bg-muted")).toBe(true);
    expect(notice.querySelector("a")).toBeNull();
    expect(readSpy).toHaveBeenCalledWith("/issues/task/runs");
  });

  it("keeps the required next action for other reconciliation causes", () => {
    render(
      <ExecutionBlockerNotice
        issueId="task"
        blocker={{ ...RETRYABLE, cause: "action_outcome_unknown", nextAction: "Verify the external action outcome before continuing." }}
      />,
    );
    expect(screen.getByRole("status").textContent).toContain("Verify the external action outcome before continuing.");
    expect(screen.getByRole("status").textContent).not.toContain("Automatic recovery of this task stopped.");
  });

  it("hides Retry when the blocking run is not a failed run", () => {
    readSpy.mockReturnValue({ data: { runs: [{ runId: "failed-run", agentId: "agent", status: "running" }] } });
    render(<ExecutionBlockerNotice issueId="task" blocker={RETRYABLE} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("retries the exact failed run and refreshes the task", async () => {
    postSpy.mockResolvedValue({ id: "new-run" });
    const onRetried = vi.fn();
    render(<ExecutionBlockerNotice issueId="task" blocker={RETRYABLE} onRetried={onRetried} />);
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

  it("shows a failed Retry in the same container and allows another attempt", async () => {
    postSpy.mockRejectedValue(new Error("Environment cleanup is still running."));
    const onRetried = vi.fn();
    render(<ExecutionBlockerNotice issueId="task" blocker={RETRYABLE} onRetried={onRetried} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Environment cleanup is still running.");
    expect(screen.getByRole("button", { name: "Retry" })).not.toBeDisabled();
    expect(onRetried).not.toHaveBeenCalled();
  });

  it("treats a skipped wakeup as a failed retry", async () => {
    postSpy.mockResolvedValue({ status: "skipped", message: "Agent is paused." });
    render(<ExecutionBlockerNotice issueId="task" blocker={RETRYABLE} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Agent is paused.");
  });

  it("asks what a stopped run got done instead of offering a Retry that Paperclip refuses", () => {
    render(<ExecutionBlockerNotice issueId="task" blocker={LEGACY_BLOCKER} />);
    const notice = screen.getByRole("status", { name: "Task recovery" });
    expect(notice).toHaveTextContent("This run was stopped. Did it finish what it was doing?");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    for (const name of ["Nothing done", "Partly", "Done"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "Resume" })).toBeDisabled();
  });

  it.each([
    ["Nothing done", "not_performed", "", "A person answered: nothing done."],
    ["Partly", "mixed", " wrote half the file ", "A person answered: partly. wrote half the file"],
    ["Done", "completed", "", "A person answered: done."],
  ])("Resume after %s resolves the recovery action with executionReconciliation", async (label, actionOutcome, note, evidence) => {
    postSpy.mockResolvedValue({ issue: { id: "task" }, recoveryAction: { id: "recovery" } });
    const onRetried = vi.fn();
    render(<ExecutionBlockerNotice issueId="task" blocker={LEGACY_BLOCKER} onRetried={onRetried} />);
    fireEvent.click(screen.getByRole("button", { name: label }));
    if (note) fireEvent.change(screen.getByRole("textbox"), { target: { value: note } });
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(onRetried).toHaveBeenCalledOnce());
    expect(postSpy).toHaveBeenCalledWith("/issues/task/recovery-actions/resolve", {
      actionId: "recovery",
      outcome: "restored",
      sourceIssueStatus: "todo",
      executionReconciliation: { runId: "failed-run", providerStopped: true, actionOutcome, outcomeEvidence: evidence },
    });
    expect(evidence.length).toBeGreaterThanOrEqual(20);
    expect(mutateSpy).toHaveBeenCalledWith("/teams/issues/task/recovery-actions");
  });

  it("shows Paperclip's refusal and keeps Resume available", async () => {
    postSpy.mockRejectedValue(new Error("The previous provider is still running. Stop it before continuing."));
    render(<ExecutionBlockerNotice issueId="task" blocker={LEGACY_BLOCKER} />);
    fireEvent.click(screen.getByRole("button", { name: "Partly" }));
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The previous provider is still running.");
    expect(screen.getByRole("button", { name: "Resume" })).not.toBeDisabled();
  });
});
