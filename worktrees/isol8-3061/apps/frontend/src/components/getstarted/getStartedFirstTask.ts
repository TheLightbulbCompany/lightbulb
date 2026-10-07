import type { CompanyAgent } from "@/components/teams/shared/types";

// Pre-filled scaffold for the Get Started "Create your first task" step. The
// user edits it before hitting Create — the point is a populated, CEO-directed
// first task instead of a blank composer. "[your goal]" is an obvious
// replace-me token left in the editable fields.
export const ONBOARDING_FIRST_TASK_TITLE = "Build out my team for [your goal]";

export const ONBOARDING_FIRST_TASK_DESCRIPTION = `You are my CEO. Help me stand up a team to deliver [your goal].

- Work out what roles and skills this goal needs.
- Break it into concrete tasks and start delegating.
- If a needed skill isn't on the team yet, bring it in.
- Report back with the plan and the first tasks you've kicked off.`;

// Resolve the backend-authored starter (or legacy seed) root CEO so the first
// task is assigned to the orchestrator without depending on its display name.
// Returns undefined while that identity is unavailable; the dialog then opens
// unassigned.
export function resolveCeoAgentId(agents: CompanyAgent[]): string | undefined {
  const byRole = agents.find(
    (a) =>
      a.role === "ceo" &&
      !a.reportsTo &&
      (a.origin === "Starter" || a.origin === "Seed"),
  );
  return byRole?.id;
}
