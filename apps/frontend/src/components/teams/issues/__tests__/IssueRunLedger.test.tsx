// Adapted from upstream Paperclip's IssueRunLedger.test.tsx
// (paperclip/ui/src/components/IssueRunLedger.test.tsx @ v2026.916.1-isol8.2).
// Dropped: the activity-interleave case (no activity feed here).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render as rtlRender, fireEvent } from "@testing-library/react";
import type { ComponentProps } from "react";

const { readSpy, postSpy, mutateSpy } = vi.hoisted(() => ({
  readSpy: vi.fn(),
  postSpy: vi.fn(),
  mutateSpy: vi.fn(),
}));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read: readSpy, post: postSpy, mutate: mutateSpy }),
}));
vi.mock("@/lib/notify", () => ({ notifyError: vi.fn() }));

import { IssueRunLedger, IssueRunLedgerContent } from "@/components/teams/issues/IssueRunLedger";
import type { Issue, IssueLiveRun, IssueRun, RunLivenessState } from "@/components/teams/shared/types";

let container: HTMLElement;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-04-18T20:00:00.000Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

function createRun(overrides: Partial<IssueRun> = {}): IssueRun {
  return {
    runId: "run-00000000",
    status: "succeeded",
    agentId: "agent-1",
    startedAt: "2026-04-18T19:58:00.000Z",
    finishedAt: "2026-04-18T19:59:00.000Z",
    createdAt: "2026-04-18T19:58:00.000Z",
    invocationSource: "assignment",
    resultJson: null,
    livenessState: "advanced",
    livenessReason: "Run produced concrete action evidence: 2 activity event(s)",
    continuationAttempt: 0,
    lastUsefulActionAt: "2026-04-18T19:59:00.000Z",
    nextAction: null,
    ...overrides,
  };
}

function createIssue(overrides: Partial<Issue> = {}): Issue {
  return {
    id: "issue-1",
    identifier: "PAP-1",
    title: "Child issue",
    status: "todo",
    ...overrides,
  } as Issue;
}

function createActiveRun(overrides: Partial<IssueLiveRun> = {}): IssueLiveRun {
  return {
    id: "run-live-1",
    status: "running",
    invocationSource: "assignment",
    startedAt: "2026-04-18T19:58:00.000Z",
    finishedAt: null,
    createdAt: "2026-04-18T19:58:00.000Z",
    agentId: "agent-1",
    agentName: "CodexCoder",
    adapterType: "codex_local",
    outputSilence: {
      silenceAgeMs: 45 * 60 * 1000,
      level: "critical",
      evaluationIssueId: "issue-eval-1",
      evaluationIssueIdentifier: "PAP-404",
    },
    ...overrides,
  };
}

function renderLedger(props: Partial<ComponentProps<typeof IssueRunLedgerContent>> = {}) {
  const result = rtlRender(
    <IssueRunLedgerContent
      runs={props.runs ?? []}
      liveRuns={props.liveRuns}
      activeRun={props.activeRun}
      issueStatus={props.issueStatus ?? "in_progress"}
      childIssues={props.childIssues ?? []}
      agentMap={props.agentMap ?? new Map([["agent-1", { name: "CodexCoder" }]])}
      resolveUserLabel={props.resolveUserLabel}
      pendingWatchdogDecision={props.pendingWatchdogDecision}
      canRecordWatchdogDecisions={props.canRecordWatchdogDecisions}
      watchdogDecisionError={props.watchdogDecisionError}
      onWatchdogDecision={props.onWatchdogDecision}
    />,
  );
  container = result.container;
  return result;
}

function buttonWithText(text: string) {
  return Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes(text));
}

describe("IssueRunLedger (ported)", () => {
  it("renders every liveness state with exhausted continuation context", () => {
    const states: RunLivenessState[] = [
      "advanced", "plan_only", "empty_response", "blocked", "failed", "completed", "needs_followup",
    ];
    renderLedger({
      runs: states.map((state, index) =>
        createRun({
          runId: `run-${index}0000000`,
          createdAt: `2026-04-18T19:5${index}:00.000Z`,
          livenessState: state,
          livenessReason: state === "needs_followup"
            ? "Run produced useful output but no concrete action evidence; continuation attempts exhausted"
            : `state ${state}`,
          continuationAttempt: state === "needs_followup" ? 3 : 0,
        }),
      ),
    });
    for (const label of ["Advanced", "Plan only", "Empty response", "Blocked", "Failed", "Completed", "Needs follow-up", "Exhausted", "Continuation attempt 3"]) {
      expect(container.textContent).toContain(label);
    }
  });

  it("renders historical runs without liveness metadata as unavailable", () => {
    renderLedger({
      runs: [createRun({ livenessState: null, livenessReason: null, continuationAttempt: undefined, lastUsefulActionAt: null })],
    });
    expect(container.textContent).toContain("No liveness data");
    expect(container.textContent).toContain("Stop Unavailable");
    expect(container.textContent).toContain("Last useful action Unavailable");
  });

  it("shows live runs as pending final checks without missing-data language", () => {
    renderLedger({
      runs: [createRun({ status: "running", finishedAt: null, livenessState: null, livenessReason: null, lastUsefulActionAt: null })],
    });
    expect(container.textContent).toContain("Running now by CodexCoder");
    expect(container.textContent).toContain("Checks after finish");
    expect(container.textContent).toContain("Last useful action No action recorded yet");
    expect(container.textContent).toContain("Stop Still running");
  });

  it("surfaces scheduled retry timing and exhaustion state without opening logs", () => {
    renderLedger({
      runs: [
        createRun({
          runId: "run-scheduled", status: "scheduled_retry", finishedAt: null, livenessState: null, livenessReason: null,
          retryOfRunId: "run-root", scheduledRetryAt: "2026-04-18T20:15:00.000Z", scheduledRetryAttempt: 2,
          scheduledRetryReason: "transient_failure",
        }),
        createRun({
          runId: "run-exhausted", status: "failed", createdAt: "2026-04-18T19:57:00.000Z", retryOfRunId: "run-root",
          scheduledRetryAttempt: 4, scheduledRetryReason: "transient_failure",
          retryExhaustedReason: "Bounded retry exhausted after 4 scheduled attempts; no further automatic retry will be queued",
        }),
      ],
    });
    for (const text of ["Retry scheduled", "Attempt 2", "Transient failure", "Next retry", "Retry exhausted", "no further automatic retry will be queued", "Manual intervention required"]) {
      expect(container.textContent).toContain(text);
    }
    // Retry-of links stay in-shell.
    const retryOf = Array.from(container.querySelectorAll("a")).find((a) => a.textContent === "run-root");
    expect(retryOf?.getAttribute("href")).toBe("/chat?panel=teams-run-detail&detailId=run-root");
  });

  it("labels max-turn stops and continuation retries", () => {
    renderLedger({
      runs: [
        createRun({
          runId: "run-scheduled-continuation", status: "scheduled_retry", finishedAt: null, livenessState: null,
          retryOfRunId: "run-max-turns", scheduledRetryAt: "2026-04-18T20:15:00.000Z", scheduledRetryAttempt: 1,
          scheduledRetryReason: "max_turns_continuation",
        }),
        createRun({ runId: "run-max-turns", resultJson: { stopReason: "max_turns_exhausted" }, createdAt: "2026-04-18T19:57:00.000Z" }),
      ],
    });
    expect(container.textContent).toContain("Continuation scheduled");
    expect(container.textContent).toContain("Max-turn continuation");
    expect(container.textContent).toContain("Next continuation");
    expect(container.textContent).toContain("Stop max turns exhausted");
  });

  it("shows timeout, cancel, and budget stop reasons without raw logs", () => {
    renderLedger({
      runs: [
        createRun({ runId: "run-timeout", resultJson: { stopReason: "timeout", timeoutFired: true, effectiveTimeoutSec: 30 } }),
        createRun({ runId: "run-cancel", resultJson: { stopReason: "cancelled" }, createdAt: "2026-04-18T19:57:00.000Z" }),
        createRun({ runId: "run-budget", resultJson: { stopReason: "budget_paused" }, createdAt: "2026-04-18T19:56:00.000Z" }),
        createRun({ runId: "run-paused", resultJson: { stopReason: "paused" }, createdAt: "2026-04-18T19:55:00.000Z" }),
      ],
    });
    for (const text of ["timeout (30s timeout)", "cancelled", "budget paused", "paused by board"]) {
      expect(container.textContent).toContain(text);
    }
  });

  it("surfaces active and completed child issue summaries with in-shell links", () => {
    const { unmount } = renderLedger({
      childIssues: [
        createIssue({ id: "child-1", identifier: "PAP-2", title: "Implement worker handoff", status: "in_progress" }),
        createIssue({ id: "child-2", identifier: "PAP-3", title: "Verify final report", status: "done" }),
        createIssue({ id: "child-3", identifier: "PAP-4", title: "Cancelled experiment", status: "cancelled" }),
      ],
    });
    expect(container.textContent).toContain("Child work");
    expect(container.textContent).toContain("1 active, 1 done, 1 cancelled");
    const childLink = Array.from(container.querySelectorAll("a")).find((a) => a.textContent?.includes("PAP-2"));
    expect(childLink?.getAttribute("href")).toBe("/chat?panel=teams-issue-detail&detailId=child-1");
    unmount();

    renderLedger({
      childIssues: [
        createIssue({ id: "child-2", identifier: "PAP-3", status: "done" }),
        createIssue({ id: "child-3", identifier: "PAP-4", status: "cancelled" }),
      ],
    });
    expect(container.textContent).toContain("all 2 terminal (1 done, 1 cancelled)");
  });

  it("uses wrapping-friendly markup for long next action text", () => {
    renderLedger({ runs: [createRun({ nextAction: "Continue investigating this intentionally-long-next-action-token." })] });
    const nextAction = Array.from(container.querySelectorAll("span")).find((n) => n.textContent?.includes("intentionally-long-next-action-token"));
    expect(nextAction?.className).toContain("break-words");
    expect(container.textContent).toContain("Next action:");
  });

  it("shows when older runs are clipped from the ledger", () => {
    renderLedger({
      runs: Array.from({ length: 22 }, (_, index) =>
        createRun({ runId: `run-${index.toString().padStart(8, "0")}`, createdAt: `2026-04-18T19:${String(index).padStart(2, "0")}:00.000Z` }),
      ),
    });
    expect(container.textContent).toContain("2 older items not shown");
  });

  it("renders evaluation context with watchdog actions and a silence badge", () => {
    const onWatchdogDecision = vi.fn();
    renderLedger({
      runs: [createRun({ runId: "run-live-1", status: "running", finishedAt: null })],
      activeRun: createActiveRun(),
      onWatchdogDecision,
    });
    expect(container.textContent).toContain("Critical output silence");
    expect(container.textContent).toContain("PAP-404");
    expect(container.textContent).toContain("Critical silence");
    expect(container.textContent).toContain("Paperclip did not create new delegated recovery work");
    const banner = Array.from(container.querySelectorAll("p"))
      .find((n) => n.textContent?.includes("Critical output silence"))?.closest("div");
    expect(banner?.className).toContain("border-err/30");
    expect(banner?.className).toContain("bg-err/10");
    const evalLink = Array.from(container.querySelectorAll("a")).find((a) => a.textContent === "PAP-404");
    expect(evalLink?.getAttribute("href")).toBe("/chat?panel=teams-issue-detail&detailId=issue-eval-1");

    fireEvent.click(buttonWithText("Continue monitoring")!);
    expect(onWatchdogDecision).toHaveBeenCalledWith({
      runId: "run-live-1",
      decision: "continue",
      evaluationIssueId: "issue-eval-1",
    });
  });

  it.each([
    { level: "suspicious" as const, heading: "Output silence watchdog warning", badge: "Output silence" },
    { level: "critical" as const, heading: "Critical output silence", badge: "Critical silence" },
  ])("renders a $level UI-only signal without an evaluation-task link", ({ level, heading, badge }) => {
    const onWatchdogDecision = vi.fn();
    renderLedger({
      runs: [createRun({ runId: "run-live-1", status: "running", finishedAt: null })],
      activeRun: createActiveRun({
        outputSilence: { level, silenceAgeMs: 45 * 60 * 1000, evaluationIssueId: null, evaluationIssueIdentifier: null },
      }),
      onWatchdogDecision,
    });
    expect(container.textContent).toContain(heading);
    expect(container.textContent).toContain(badge);
    expect(container.textContent).toContain("Paperclip did not create or assign a recovery task");
    expect(container.textContent).not.toContain("PAP-404");
    for (const text of ["Continue monitoring", "Snooze 1h", "Mark false positive"]) {
      expect(container.textContent).toContain(text);
    }
    fireEvent.click(buttonWithText("Continue monitoring")!);
    expect(onWatchdogDecision).toHaveBeenCalledWith({ runId: "run-live-1", decision: "continue", evaluationIssueId: null });
  });

  it("hides watchdog decision actions for known non-owner viewers", () => {
    const onWatchdogDecision = vi.fn();
    renderLedger({
      runs: [createRun({ runId: "run-live-1", status: "running", finishedAt: null })],
      activeRun: createActiveRun(),
      canRecordWatchdogDecisions: false,
      onWatchdogDecision,
    });
    expect(container.textContent).toContain("Critical output silence");
    expect(container.textContent).not.toContain("Continue monitoring");
    expect(onWatchdogDecision).not.toHaveBeenCalled();
  });

  it("surfaces the responsible user a run acts on behalf of", () => {
    renderLedger({
      runs: [createRun({ runId: "run-obo-1", responsibleUserId: "user-2" })],
      resolveUserLabel: (userId) => (userId === "user-2" ? "Ada Lovelace" : null),
    });
    const chip = container.querySelector('[data-testid="run-on-behalf-of"]');
    expect(chip?.textContent).toContain("on behalf of");
    expect(chip?.textContent).toContain("Ada Lovelace");
  });

  it("renders actionable copy for responsible-user denials only", () => {
    const { unmount } = renderLedger({
      runs: [createRun({ runId: "run-denied-1", status: "failed", livenessState: "failed", responsibleUserId: "user-2", errorCode: "RESPONSIBLE_USER_UNAUTHORIZED" })],
      resolveUserLabel: () => "Ada Lovelace",
    });
    const notice = container.querySelector('[data-testid="responsible-user-denial-notice"]');
    expect(notice?.getAttribute("data-denial-tone")).toBe("unauthorized");
    expect(notice?.textContent).toContain("Ada Lovelace");
    expect(notice?.textContent).toContain("Responsible user not authorized");
    unmount();

    const second = renderLedger({
      runs: [createRun({ runId: "run-denied-2", status: "failed", livenessState: "failed", responsibleUserId: "user-3", errorCode: "RESPONSIBLE_USER_UNAVAILABLE" })],
    });
    expect(container.querySelector('[data-testid="responsible-user-denial-notice"]')?.getAttribute("data-denial-tone")).toBe("unavailable");
    second.unmount();

    renderLedger({ runs: [createRun({ runId: "run-denied-3", status: "failed", errorCode: "budget_blocked" })] });
    expect(container.querySelector('[data-testid="responsible-user-denial-notice"]')).toBeNull();
  });

  it("marks a source-resolved watchdog fold", () => {
    renderLedger({
      runs: [createRun({
        resultJson: {
          sourceResolvedWatchdogFold: {
            sourceIssueId: "iss", sourceIssueStatus: "done", sameRunEvidenceKind: "comment",
            sameRunEvidenceId: "c1", sameRunEvidenceAt: "2026-04-18T19:59:00.000Z",
          },
        },
      })],
    });
    expect(container.querySelector('[aria-label="Source-resolved watchdog fold"]')).not.toBeNull();
  });
});

describe("IssueRunLedger container", () => {
  beforeEach(() => {
    readSpy.mockReset();
    postSpy.mockReset();
    mutateSpy.mockReset();
  });

  function stubReads(activeRun: IssueLiveRun | null) {
    readSpy.mockImplementation((path: string) => {
      if (path.endsWith("/runs")) return { data: { runs: [createRun({ runId: "run-live-1", status: "running", finishedAt: null })] } };
      if (path.endsWith("/active-run")) return { data: activeRun };
      if (path.endsWith("/children")) return { data: { issues: [] } };
      return { data: [] };
    });
  }

  it("reads the issue-scoped BFF keys and records watchdog decisions through the run route", async () => {
    vi.useRealTimers();
    stubReads(createActiveRun());
    postSpy.mockResolvedValue({});
    const result = rtlRender(<IssueRunLedger issueId="iss_1" issueStatus="in_progress" agents={[]} />);
    container = result.container;
    expect(readSpy.mock.calls.map((c) => c[0])).toEqual(
      expect.arrayContaining(["/issues/iss_1/runs", "/issues/iss_1/live-runs", "/issues/iss_1/active-run", "/issues/iss_1/children"]),
    );
    fireEvent.click(buttonWithText("Mark false positive")!);
    expect(postSpy).toHaveBeenCalledWith("/heartbeat-runs/run-live-1/watchdog-decisions", {
      decision: "dismissed_false_positive",
      evaluationIssueId: "issue-eval-1",
      reason: "Dismissed from issue run ledger",
      snoozedUntil: null,
    });
    await vi.waitFor(() => expect(mutateSpy).toHaveBeenCalledWith("/teams/issues/iss_1/active-run"));
  });

  it("tolerates drifted BFF bodies: a non-run active-run body and a status-less run row", () => {
    readSpy.mockImplementation((path: string) => {
      if (path.endsWith("/runs")) return { data: { runs: [{ runId: "run-odd", agentId: "agent-1" }] } };
      if (path.endsWith("/active-run")) return { data: {} };
      return { data: { issues: [] } };
    });
    const result = rtlRender(<IssueRunLedger issueId="iss_1" issueStatus="todo" agents={[]} />);
    expect(result.container.textContent).toContain("unknown by agent-1");
  });

  it("shows the board-only copy when Paperclip refuses the decision", async () => {
    vi.useRealTimers();
    stubReads(createActiveRun());
    postSpy.mockRejectedValue(Object.assign(new Error("Forbidden"), { status: 403 }));
    const result = rtlRender(<IssueRunLedger issueId="iss_1" issueStatus="in_progress" agents={[]} />);
    container = result.container;
    fireEvent.click(buttonWithText("Continue monitoring")!);
    await vi.waitFor(() =>
      expect(container.textContent).toContain("Only the board or the assigned recovery owner can record watchdog decisions"),
    );
  });
});
