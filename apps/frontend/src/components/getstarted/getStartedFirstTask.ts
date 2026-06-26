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

// Resolve the seeded CEO (Friday) so the onboarding first task is assigned to
// the orchestrator. `seeded` is server-stamped from the persisted seeded-agent
// IDs (never inferred from role/name), so a user-created agent named "Friday"
// or using role "ceo" is never matched. Returns undefined when no seeded CEO is
// present (e.g. roster not loaded yet) — the dialog then opens unassigned.
export function resolveCeoAgentId(agents: CompanyAgent[]): string | undefined {
  const byRole = agents.find((a) => a.seeded === true && a.role === "ceo");
  if (byRole) return byRole.id;
  const byName = agents.find(
    (a) => a.seeded === true && a.name.toLowerCase() === "friday",
  );
  return byName?.id;
}
