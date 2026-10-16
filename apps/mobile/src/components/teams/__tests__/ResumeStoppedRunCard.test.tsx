import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

// teamsData imports expo-router (useFocusEffect) at module level.
jest.mock("expo-router", () => ({ useFocusEffect: jest.fn() }));

import { needsResumeAnswer, type ExecutionBlocker } from "../../../lib/teamsData";
import { ResumeStoppedRunCard } from "../ResumeStoppedRunCard";

// The blocker an operator Stop leaves on dev (WAL-16, 2026-10-16).
const STOPPED: ExecutionBlocker = {
  recoveryActionId: "recovery",
  runId: "stopped-run",
  agentId: "agent",
  cause: "legacy_execution_requires_reconciliation",
  nextAction: "Inspect the stopped provider and recorded actions, then reconcile their outcomes before continuing.",
  canContinue: false,
  canRetry: false,
};

describe("needsResumeAnswer", () => {
  it("is true only for a reconciliation-class stop Paperclip will not Retry", () => {
    expect(needsResumeAnswer(STOPPED)).toBe(true);
    expect(needsResumeAnswer({ ...STOPPED, canRetry: true })).toBe(false);
    expect(needsResumeAnswer({ ...STOPPED, cause: "action_outcome_unknown" })).toBe(false);
    expect(needsResumeAnswer({ ...STOPPED, recoveryActionId: null })).toBe(false);
    expect(needsResumeAnswer(null)).toBe(false);
  });
});

describe("ResumeStoppedRunCard", () => {
  it.each([
    ["Nothing done", "not_performed", "", "A person answered: nothing done."],
    ["Partly", "mixed", " wrote half the file ", "A person answered: partly. wrote half the file"],
    ["Done", "completed", "", "A person answered: done."],
  ])("Resume after %s posts the executionReconciliation body", async (label, actionOutcome, note, evidence) => {
    const onResume = jest.fn().mockResolvedValue(null);
    await render(<ResumeStoppedRunCard blocker={STOPPED} onResume={onResume} />);
    expect(screen.getByText("This run was stopped. Did it finish what it was doing?")).toBeTruthy();
    fireEvent.press(screen.getByLabelText(label));
    if (note) fireEvent.changeText(screen.getByLabelText("Note for the agent (optional)"), note);
    fireEvent.press(screen.getByLabelText("Resume"));
    await waitFor(() => expect(onResume).toHaveBeenCalledTimes(1));
    expect(onResume).toHaveBeenCalledWith({
      actionId: "recovery",
      outcome: "restored",
      sourceIssueStatus: "todo",
      executionReconciliation: { runId: "stopped-run", providerStopped: true, actionOutcome, outcomeEvidence: evidence },
    });
  });

  it("does nothing until an answer is chosen, and shows Paperclip's refusal", async () => {
    const onResume = jest.fn().mockResolvedValue("The previous provider is still running.");
    await render(<ResumeStoppedRunCard blocker={STOPPED} onResume={onResume} />);
    fireEvent.press(screen.getByLabelText("Resume"));
    expect(onResume).not.toHaveBeenCalled();
    fireEvent.press(screen.getByLabelText("Partly"));
    fireEvent.press(screen.getByLabelText("Resume"));
    expect(await screen.findByText("The previous provider is still running.")).toBeTruthy();
  });
});
